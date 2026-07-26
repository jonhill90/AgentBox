#!/usr/bin/env python3
"""
Local auth layer (SPEC.md §12 / PRD 1.10).

Two halves:

  - With AGENTBOX_AUTH_TOKEN unset — the default the rest of the suite
    runs under — nothing is gated. The whole existing suite passing
    unchanged is the real proof of that; the checks here just pin the
    contract explicitly.
  - With it set, a second container is started with the token
    configured (same pattern as the §10 toggle tests) and everything is
    exercised against it: no token, wrong token, right token.

`/health` must work in every one of those cases, since it is the Docker
healthcheck target rather than a capability.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import CONTAINER, requires_auth_off, requires_docker_introspection, rest_get

TOKEN = "test-token-1a2b3c4d"
WRONG_TOKEN = "test-token-wrong"

AUTH_CONTAINER = "agentbox-auth-test"
AUTH_PORT = 8057
AUTH_URL = f"http://localhost:{AUTH_PORT}"


# ── helpers ──────────────────────────────────────────────────────────

def _request(url: str, method: str = "GET", body: dict | None = None,
             token: str | None = None) -> tuple[int, dict | str]:
    """HTTP call that never raises on 4xx. Returns (status, decoded body)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw, ctype = resp.read().decode(), resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as exc:
        raw, ctype = exc.read().decode(), exc.headers.get("Content-Type", "")
        return exc.code, (json.loads(raw) if "json" in ctype else raw)


def _wait_for(url: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(1)
    raise RuntimeError(f"{url} never became healthy: {last}")


@pytest.fixture(scope="module")
def authed_server():
    """A second AgentBox container with AGENTBOX_AUTH_TOKEN configured."""
    image = subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    subprocess.run(["docker", "rm", "-f", AUTH_CONTAINER], capture_output=True, check=False)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", AUTH_CONTAINER,
            "-e", f"AGENTBOX_AUTH_TOKEN={TOKEN}",
            "-p", f"{AUTH_PORT}:8000", image,
        ],
        capture_output=True, text=True, check=True,
    )
    try:
        _wait_for(AUTH_URL)
        yield AUTH_URL
    finally:
        subprocess.run(["docker", "rm", "-f", AUTH_CONTAINER], capture_output=True, check=False)


async def _call_tool(url: str, name: str, args: dict, token: str | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(f"{url}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, args)
            return json.loads(result.content[0].text)


# ── auth OFF: the default this whole suite runs under ────────────────

@requires_auth_off
async def test_auth_reports_itself_off_by_default():
    status, body = await rest_get("/api/auth-required")
    assert status == 200, body
    assert body["auth_required"] is False, (
        "the default container has auth on — every other test in this suite "
        "assumes it is off"
    )


@requires_auth_off
async def test_api_routes_need_no_token_by_default():
    status, body = await rest_get("/api/screenshot")
    assert status in (200, 404), f"unexpected gate on the default build: {status} {body}"


# ── auth ON: /health and the probe stay open ─────────────────────────

@requires_docker_introspection
def test_health_is_open_with_auth_on(authed_server):
    status, body = _request(f"{authed_server}/health")
    assert status == 200, body
    assert body["status"] == "healthy", body


@requires_docker_introspection
def test_health_also_accepts_a_token(authed_server):
    status, body = _request(f"{authed_server}/health", token=TOKEN)
    assert status == 200, body


@requires_docker_introspection
def test_auth_required_probe_is_open_and_reports_true(authed_server):
    status, body = _request(f"{authed_server}/api/auth-required")
    assert status == 200, body
    assert body["auth_required"] is True, body


@requires_docker_introspection
def test_ui_page_still_loads_unauthenticated(authed_server):
    """The page must load before it can ask for a token."""
    status, body = _request(f"{authed_server}/ui")
    assert status == 200, status
    assert "Take Control" in body


# ── auth ON: REST routes are gated ───────────────────────────────────

@requires_docker_introspection
def test_rest_route_rejects_a_missing_token(authed_server):
    status, body = _request(
        f"{authed_server}/api/browser/navigate", "POST", {"url": "https://example.com/"}
    )
    assert status == 401, f"{status} {body}"
    assert body["success"] is False, body
    assert body["status"] == 401, body
    assert "Unauthorized" in body["error"], body


@requires_docker_introspection
def test_rest_route_rejects_a_wrong_token(authed_server):
    status, body = _request(
        f"{authed_server}/api/browser/navigate", "POST",
        {"url": "https://example.com/"}, token=WRONG_TOKEN,
    )
    assert status == 401, f"{status} {body}"
    assert body["success"] is False and "Unauthorized" in body["error"], body


@requires_docker_introspection
def test_rest_route_rejects_a_malformed_authorization_header(authed_server):
    """The `Bearer ` prefix is required, as in Hill90's _check_auth."""
    req = urllib.request.Request(
        f"{authed_server}/api/browser/navigate",
        data=json.dumps({"url": "https://example.com/"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": TOKEN},  # no prefix
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            pytest.fail(f"raw token accepted without the Bearer prefix: {resp.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


@requires_docker_introspection
def test_rest_route_accepts_the_right_token(authed_server):
    status, body = _request(
        f"{authed_server}/api/browser/navigate", "POST",
        {"url": "https://example.com/"}, token=TOKEN,
    )
    assert status == 200, f"{status} {body}"
    assert body["success"] is True, body
    assert body["title"] == "Example Domain", body


@requires_docker_introspection
def test_every_api_route_is_gated(authed_server):
    """Not just the one route the other tests happen to use."""
    routes = [
        ("GET", "/api/screenshot", None),
        ("POST", "/api/browser/navigate", {"url": "https://example.com/"}),
        ("POST", "/api/browser/click", {"x_percent": 50, "y_percent": 50}),
        ("POST", "/api/browser/scroll", {"delta_y": 10}),
        ("POST", "/api/browser/keypress", {"key": "End"}),
        ("POST", "/api/browser/type", {"text": "x"}),
        ("POST", "/api/browser/element", {"x_percent": 50, "y_percent": 50}),
        ("POST", "/api/browser/history", {"action": "reload"}),
    ]
    for method, path, body in routes:
        status, payload = _request(f"{authed_server}{path}", method, body)
        assert status == 401, f"{path} was reachable without a token: {status} {payload}"


# ── auth ON: MCP tools are gated ─────────────────────────────────────

@requires_docker_introspection
async def test_mcp_tool_rejects_a_missing_token(authed_server):
    result = await _call_tool(authed_server, "navigate", {"url": "https://example.com/"})
    assert result["success"] is False, result
    assert result["status"] == 401, result
    assert "Unauthorized" in result["error"], result


@requires_docker_introspection
async def test_mcp_tool_rejects_a_wrong_token(authed_server):
    result = await _call_tool(
        authed_server, "navigate", {"url": "https://example.com/"}, token=WRONG_TOKEN
    )
    assert result["success"] is False and result["status"] == 401, result


@requires_docker_introspection
async def test_mcp_tool_accepts_the_right_token(authed_server):
    result = await _call_tool(
        authed_server, "navigate", {"url": "https://example.com/"}, token=TOKEN
    )
    assert result["success"] is True, result
    assert result["title"] == "Example Domain", result


@requires_docker_introspection
async def test_a_gated_jumpbox_tool_is_also_covered(authed_server):
    """Auth and the §10 toggle are independent gates; both apply."""
    denied = await _call_tool(authed_server, "read_file", {"path": "/workspace"})
    assert denied["success"] is False and denied["status"] == 401, denied

    allowed = await _call_tool(authed_server, "read_file", {"path": "/workspace"}, token=TOKEN)
    assert allowed["status"] != 401 if "status" in allowed else True, allowed
    assert "Unauthorized" not in str(allowed.get("error", "")), allowed


@requires_docker_introspection
async def test_health_tool_is_gated_even_though_the_route_is_not(authed_server):
    """GET /health is the healthcheck target; the MCP `health` tool is not."""
    denied = await _call_tool(authed_server, "health", {})
    assert denied["success"] is False and denied["status"] == 401, denied

    allowed = await _call_tool(authed_server, "health", {}, token=TOKEN)
    assert allowed["status"] == "healthy", allowed


@requires_docker_introspection
async def test_unauthorized_answer_never_leaks_browser_state(authed_server):
    """A refusal must not double as an oracle for what the page is doing."""
    result = await _call_tool(authed_server, "screenshot", {})
    assert set(result) == {"success", "status", "error"}, result


@requires_docker_introspection
def test_startup_log_states_that_auth_is_on(authed_server):
    logs = subprocess.run(
        ["docker", "logs", AUTH_CONTAINER], capture_output=True, text=True, check=True,
    )
    combined = logs.stdout + logs.stderr
    assert "Auth ENABLED" in combined, combined[-2000:]


@requires_auth_off
def test_default_container_logs_auth_disabled():
    logs = subprocess.run(
        ["docker", "logs", CONTAINER], capture_output=True, text=True, check=True,
    )
    combined = logs.stdout + logs.stderr
    assert "Auth DISABLED" in combined, combined[-2000:]
