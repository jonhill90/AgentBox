#!/usr/bin/env python3
"""
AgentBox Phase 1 integration test (SPEC.md §6).

Starts the container, connects a real MCP client over Streamable HTTP,
and proves the persistent-browser-loop pattern actually works:

  navigate -> screenshot (non-empty PNG) -> more calls on the SAME page

"Same page" is proven two independent ways:
  1. A JS global set in one `evaluate` call is still readable by a
     later, separate `evaluate` call.
  2. After a second `navigate`, `history back` returns to the first URL
     — a freshly-created page would have no session history to go back
     through.

Run with the container already up:
    AGENTBOX_URL=http://localhost:8054 pytest tests/test_integration.py

Or let the test bring it up itself (default):
    pytest tests/test_integration.py
"""

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO_ROOT = Path(__file__).resolve().parent.parent
PORT = os.environ.get("AGENTBOX_PORT", "8054")
BASE_URL = os.environ.get("AGENTBOX_URL", f"http://localhost:{PORT}")
MCP_URL = f"{BASE_URL}/mcp"

PAGE_ONE = "https://example.com/"
PAGE_TWO = "https://example.com/?agentbox=second"

EXPECTED_TOOLS = {
    "navigate", "screenshot", "click", "get_text", "evaluate",
    "click_at_percent", "type_at", "press_key", "scroll", "history", "health",
}


def _health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_for_health(timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_ok():
            return
        time.sleep(2)
    raise RuntimeError(f"AgentBox did not become healthy at {BASE_URL}/health within {timeout}s")


@pytest.fixture(scope="session", autouse=True)
def agentbox_container():
    """Ensure a running AgentBox container for the duration of the session."""
    if _health_ok():
        yield  # Already running (e.g. started by hand); leave it alone.
        return

    subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=REPO_ROOT, check=True,
    )
    _wait_for_health()
    yield
    subprocess.run(["docker", "compose", "down"], cwd=REPO_ROOT, check=False)


def _payload(result) -> dict:
    """Unwrap an MCP tool result into the JSON dict the tool returned."""
    assert not result.isError, f"tool call errored: {result.content}"
    text = result.content[0].text
    return json.loads(text)


async def test_persistent_browser_session():
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # --- tool surface is exactly SPEC §5, nothing more -------------
            tools = {t.name for t in (await session.list_tools()).tools}
            assert tools == EXPECTED_TOOLS, f"unexpected tool surface: {tools}"

            # --- navigate --------------------------------------------------
            nav = _payload(await session.call_tool("navigate", {"url": PAGE_ONE}))
            assert nav["success"], nav
            assert nav["url"].startswith("https://example.com"), nav
            assert nav["status"] == 200, nav

            # --- screenshot: a real, non-empty PNG comes back --------------
            shot = _payload(await session.call_tool("screenshot", {"full_page": True}))
            assert shot["success"], shot
            png = base64.b64decode(shot["image_base64"])
            assert len(png) > 1000, f"screenshot suspiciously small: {len(png)} bytes"
            assert png.startswith(b"\x89PNG\r\n\x1a\n"), "not a PNG"

            # --- same page, proof #1: JS state survives across tool calls ---
            _payload(await session.call_tool(
                "evaluate", {"script": "() => { window.__agentbox_probe = 4242; return true; }"}
            ))
            probe = _payload(await session.call_tool(
                "evaluate", {"script": "() => window.__agentbox_probe"}
            ))
            assert probe["success"], probe
            assert probe["result"] == "4242", (
                f"page was recreated between calls — probe came back {probe['result']!r}"
            )

            # --- text read from the live page ------------------------------
            text = _payload(await session.call_tool("get_text", {"selector": "body"}))
            assert text["success"], text
            assert "Example Domain" in text["text"], text["text"][:200]

            # --- same page, proof #2: session history survives --------------
            nav2 = _payload(await session.call_tool("navigate", {"url": PAGE_TWO}))
            assert nav2["success"], nav2
            assert "agentbox=second" in nav2["url"], nav2

            back = _payload(await session.call_tool("history", {"action": "back"}))
            assert back["success"], back
            assert "agentbox=second" not in back["url"], (
                f"history back did not return to the first page: {back['url']}"
            )

            # --- second screenshot still works on that same page ------------
            shot2 = _payload(await session.call_tool("screenshot", {"full_page": False}))
            assert shot2["success"], shot2
            assert base64.b64decode(shot2["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")

            # --- health reports the browser is up ---------------------------
            hp = _payload(await session.call_tool("health", {}))
            assert hp["status"] == "healthy", hp
            assert hp["browser_started"] is True, hp


async def test_coordinate_and_scroll_tools():
    """The by-coordinate tools operate on that same live page."""
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            nav = _payload(await session.call_tool("navigate", {"url": PAGE_ONE}))
            assert nav["success"], nav

            scrolled = _payload(await session.call_tool("scroll", {"delta_y": 200}))
            assert scrolled["success"], scrolled

            clicked = _payload(await session.call_tool(
                "click_at_percent", {"x_percent": 50, "y_percent": 50}
            ))
            assert clicked["success"], clicked
            assert clicked["x"] == 640 and clicked["y"] == 360, clicked

            key = _payload(await session.call_tool("press_key", {"key": "End"}))
            assert key["success"], key
