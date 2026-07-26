#!/usr/bin/env python3
"""Shared fixtures and helpers for the AgentBox integration suite.

Every test here talks to a real container over MCP Streamable HTTP.
There are no unit tests with a mocked browser: the whole point of Phase 1
is proving a real Chromium page survives across real tool calls.
"""

import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = os.environ.get("AGENTBOX_PORT", "8054")
BASE_URL = os.environ.get("AGENTBOX_URL", f"http://localhost:{PORT}")
MCP_URL = f"{BASE_URL}/mcp"
CONTAINER = "agentbox"

def _configured_token() -> str:
    """The token this deployment is using, if any.

    The suite has to work both ways: a developer with no .env runs an
    unauthenticated container, and a developer who has switched auth on
    runs an authenticated one. Reading the same .env Compose reads means
    the tests exercise the real configuration instead of demanding a
    particular one.
    """
    token = os.environ.get("AGENTBOX_AUTH_TOKEN", "").strip()
    if token:
        return token
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("AGENTBOX_AUTH_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


AUTH_TOKEN = _configured_token()
AUTH_ON = bool(AUTH_TOKEN)


def auth_headers(extra: dict | None = None) -> dict:
    headers = dict(extra or {})
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return headers


requires_auth_off = pytest.mark.skipif(
    AUTH_ON, reason="AGENTBOX_AUTH_TOKEN is set; this asserts the unauthenticated default",
)


PAGE_ONE = "https://example.com/"
PAGE_TWO = "https://example.com/?agentbox=second"

# SPEC.md §5 — the exact tool surface. Nothing beyond this list.
EXPECTED_TOOLS = {
    "navigate", "screenshot", "click", "get_text", "evaluate",
    "click_at_percent", "type_at", "press_key", "scroll", "history", "health",
}


# ── container lifecycle ──────────────────────────────────────────────

def health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for_health(timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_ok():
            return
        time.sleep(1)
    raise RuntimeError(f"AgentBox did not become healthy at {BASE_URL}/health within {timeout}s")


@pytest.fixture(scope="session", autouse=True)
def agentbox_container():
    """Ensure a running AgentBox container for the duration of the session."""
    if health_ok():
        yield  # Already running (e.g. started by hand); leave it alone.
        return

    subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=REPO_ROOT, check=True,
    )
    wait_for_health()
    yield
    subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=False)


# ── MCP client helpers ───────────────────────────────────────────────

@asynccontextmanager
async def mcp_session():
    """Open an initialized MCP client session against the container."""
    async with streamablehttp_client(MCP_URL, headers=auth_headers() or None) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def payload(result) -> dict:
    """Unwrap an MCP tool result into the JSON dict the tool returned."""
    assert not result.isError, f"tool call errored at protocol level: {result.content}"
    return json.loads(result.content[0].text)


async def call(session, name: str, args: dict | None = None) -> dict:
    """Call a tool and return its decoded JSON payload."""
    return payload(await session.call_tool(name, args or {}))


# ── REST helpers for the viewer UI surface (SPEC §6) ─────────────────

def _request(path: str, method: str = "GET", body: dict | None = None) -> tuple[int, str, str]:
    """Return (status, body_text, content_type). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    headers = auth_headers({"Content-Type": "application/json"} if data else {})
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), exc.headers.get("Content-Type", "")


async def rest_get(path: str) -> tuple[int, dict | str]:
    """GET a REST route. JSON responses are decoded; others returned as text."""
    status, text, ctype = await asyncio.to_thread(_request, path, "GET", None)
    return status, (json.loads(text) if "json" in ctype else text)


async def rest_post(path: str, body: dict) -> tuple[int, dict]:
    """POST JSON to a REST route and decode the JSON response."""
    status, text, _ = await asyncio.to_thread(_request, path, "POST", body)
    return status, json.loads(text)


# ── container introspection (for leak checks) ────────────────────────
#
# `docker top` runs ps on the host, so it works against the slim image,
# which has no ps of its own. We deliberately do not add procps to the
# image just to satisfy a test.

def docker_introspection_available() -> bool:
    if os.environ.get("AGENTBOX_URL"):
        return False  # Pointed at some other host; local docker says nothing useful.
    try:
        return subprocess.run(
            ["docker", "inspect", CONTAINER],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_docker_introspection = pytest.mark.skipif(
    not docker_introspection_available(),
    reason="needs local `docker top`/`docker compose logs` access to this container",
)


def chromium_process_count() -> int:
    """Count live Chromium (headless_shell) processes inside the container."""
    out = subprocess.run(
        ["docker", "top", CONTAINER, "-eo", "pid,comm"],
        capture_output=True, text=True, check=True,
    ).stdout
    return sum(1 for line in out.splitlines() if "headless_shell" in line)


def browser_creation_count() -> int:
    """How many times the server has built a browser page since it started."""
    out = subprocess.run(
        ["docker", "compose", "logs", CONTAINER],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return out.count("Browser page created on persistent loop")


def restart_container() -> None:
    subprocess.run(
        ["docker", "compose", "restart", CONTAINER],
        cwd=REPO_ROOT, check=True, capture_output=True,
    )
    wait_for_health()
