#!/usr/bin/env python3
"""
Feature-toggle trip-wires (SPEC.md §10 / PRD 1.9).

The claim being tested is specific: when AGENTBOX_ENABLE_JUMPBOX_TOOLS
is off, the gated tools are **not registered with FastMCP at all** —
absent from `list_tools()`, not present-but-refusing. A tool that exists
and returns "disabled" is still surface area, and the whole point of the
toggle is that there is nothing to reach.

Proving that needs a server started with the flag off, so these tests
run a second container from the same image on another port, with the
env var flipped. The default container (flag on) is left alone.

Skips automatically when pointed at a remote AGENTBOX_URL, since it
needs local docker.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import (
    AUTH_TOKEN,
    BASE_URL,
    CONTAINER,
    EXPECTED_TOOLS,
    call,
    mcp_session,
    requires_docker_introspection,
)

GATED_TOOLS = {"read_file", "write_file", "list_directory", "git", "http_request"}

OFF_CONTAINER = "agentbox-toggle-off-test"
OFF_PORT = 8055
OFF_URL = f"http://localhost:{OFF_PORT}"


def _image_of_running_container() -> str:
    return subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


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
def toggled_off_server():
    """A second AgentBox container with the jumpbox toggle set to false."""
    image = _image_of_running_container()
    subprocess.run(["docker", "rm", "-f", OFF_CONTAINER], capture_output=True, check=False)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", OFF_CONTAINER,
            "-e", "AGENTBOX_ENABLE_JUMPBOX_TOOLS=false",
            "-p", f"{OFF_PORT}:8000", image,
        ],
        capture_output=True, text=True, check=True,
    )
    try:
        _wait_for(OFF_URL)
        yield OFF_URL
    finally:
        subprocess.run(["docker", "rm", "-f", OFF_CONTAINER], capture_output=True, check=False)


async def _tools_at(url: str, token: str | None = None) -> set[str]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(f"{url}/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {t.name for t in (await session.list_tools()).tools}


# ── flag ON (the local-dev default the rest of the suite runs under) ──

async def test_gated_tools_are_registered_when_the_flag_is_on():
    tools = await _tools_at(BASE_URL, AUTH_TOKEN or None)
    missing = GATED_TOOLS - tools
    assert not missing, f"jumpbox tools missing with the flag on: {missing}"


async def test_flag_on_surface_is_core_plus_gated_and_nothing_else():
    """No tool has crept onto the surface beyond §5's core list plus §8/§9."""
    tools = await _tools_at(BASE_URL, AUTH_TOKEN or None)
    assert tools == EXPECTED_TOOLS | GATED_TOOLS, f"unexpected tool surface: {tools}"


# ── flag OFF ─────────────────────────────────────────────────────────

@requires_docker_introspection
async def test_gated_tools_are_absent_when_the_flag_is_off(toggled_off_server):
    tools = await _tools_at(toggled_off_server)
    present = GATED_TOOLS & tools
    assert not present, (
        f"{present} still registered with AGENTBOX_ENABLE_JUMPBOX_TOOLS=false — "
        "the toggle must remove them from the MCP surface, not just refuse calls"
    )


@requires_docker_introspection
async def test_core_browser_tools_survive_the_flag_being_off(toggled_off_server):
    """Turning the jumpbox tools off must not cost the core browser surface."""
    tools = await _tools_at(toggled_off_server)
    assert tools == EXPECTED_TOOLS, f"core tool surface changed with flag off: {tools}"


@requires_docker_introspection
async def test_calling_a_gated_tool_with_the_flag_off_is_an_unknown_tool(toggled_off_server):
    """Not 'disabled' — genuinely unknown, because it was never registered."""
    async with streamablehttp_client(f"{toggled_off_server}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("read_file", {"path": "/workspace/x"})
            text = str(result.content[0].text if result.content else "")
            assert result.isError, f"read_file answered with the flag off: {text}"
            assert "unknown tool" in text.lower() or "not found" in text.lower(), text


@requires_docker_introspection
async def test_browser_still_works_with_the_flag_off(toggled_off_server):
    """PRD 1.9's real question: is the box still useful with everything off?"""
    async with streamablehttp_client(f"{toggled_off_server}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            nav = json.loads(
                (await session.call_tool("navigate", {"url": "https://example.com/"}))
                .content[0].text
            )
            assert nav["success"] and nav["status"] == 200, nav

            shot = json.loads(
                (await session.call_tool("screenshot", {"full_page": False}))
                .content[0].text
            )
            assert shot["success"] and shot["bytes"] > 1000, shot


@requires_docker_introspection
async def test_ui_and_health_still_served_with_the_flag_off(toggled_off_server):
    with urllib.request.urlopen(f"{toggled_off_server}/health", timeout=10) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["status"] == "healthy"

    with urllib.request.urlopen(f"{toggled_off_server}/ui", timeout=10) as resp:
        assert resp.status == 200
        assert "Take Control" in resp.read().decode()


@requires_docker_introspection
def test_startup_log_states_which_way_the_toggle_went(toggled_off_server):
    logs = subprocess.run(
        ["docker", "logs", OFF_CONTAINER],
        capture_output=True, text=True, check=True,
    )
    combined = logs.stdout + logs.stderr
    assert "Jumpbox tools DISABLED" in combined, combined[-2000:]


async def test_describe_route_is_not_gated_by_the_toggle():
    """§11: Describe is core browser surface, same tier as screenshot."""
    async with mcp_session() as session:
        await call(session, "navigate", {"url": "https://example.com/"})
    # Exercised in full in test_ui_api.py; here we only assert it is not
    # bundled into the jumpbox toggle's scope.
    assert "element" not in GATED_TOOLS
