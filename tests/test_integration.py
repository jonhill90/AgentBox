#!/usr/bin/env python3
"""
AgentBox Phase 1 integration test (SPEC.md §6) — the happy path.

Proves the persistent-browser-loop pattern actually works:

  navigate -> screenshot (non-empty PNG) -> more calls on the SAME page

"Same page" is proven two independent ways:
  1. A JS global set in one `evaluate` call is still readable by a
     later, separate `evaluate` call.
  2. After a second `navigate`, `history back` returns to the first URL
     — a freshly-created page would have no session history.

Run with the container already up:
    AGENTBOX_URL=http://localhost:8054 pytest tests/

Or let the suite bring it up itself (default):
    pytest tests/
"""

import base64

from conftest import EXPECTED_TOOLS, PAGE_ONE, PAGE_TWO, call, mcp_session

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def test_tool_surface_is_exactly_spec_section_5():
    async with mcp_session() as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert tools == EXPECTED_TOOLS, f"unexpected tool surface: {tools}"


async def test_persistent_browser_session():
    async with mcp_session() as session:
        # --- navigate --------------------------------------------------
        nav = await call(session, "navigate", {"url": PAGE_ONE})
        assert nav["success"], nav
        assert nav["url"].startswith("https://example.com"), nav
        assert nav["status"] == 200, nav

        # --- screenshot: a real, non-empty PNG comes back --------------
        shot = await call(session, "screenshot", {"full_page": True})
        assert shot["success"], shot
        png = base64.b64decode(shot["image_base64"])
        assert len(png) > 1000, f"screenshot suspiciously small: {len(png)} bytes"
        assert png.startswith(PNG_MAGIC), "not a PNG"

        # --- same page, proof #1: JS state survives across tool calls ---
        await call(session, "evaluate",
                   {"script": "() => { window.__agentbox_probe = 4242; return true; }"})
        probe = await call(session, "evaluate", {"script": "() => window.__agentbox_probe"})
        assert probe["success"], probe
        assert probe["result"] == "4242", (
            f"page was recreated between calls — probe came back {probe['result']!r}"
        )

        # --- text read from the live page ------------------------------
        text = await call(session, "get_text", {"selector": "body"})
        assert text["success"], text
        assert "Example Domain" in text["text"], text["text"][:200]

        # --- same page, proof #2: session history survives --------------
        nav2 = await call(session, "navigate", {"url": PAGE_TWO})
        assert nav2["success"], nav2
        assert "agentbox=second" in nav2["url"], nav2

        back = await call(session, "history", {"action": "back"})
        assert back["success"], back
        assert "agentbox=second" not in back["url"], (
            f"history back did not return to the first page: {back['url']}"
        )

        # --- second screenshot still works on that same page ------------
        shot2 = await call(session, "screenshot", {"full_page": False})
        assert shot2["success"], shot2
        assert base64.b64decode(shot2["image_base64"]).startswith(PNG_MAGIC)

        # --- health reports the browser is up ---------------------------
        hp = await call(session, "health")
        assert hp["status"] == "healthy", hp
        assert hp["browser_started"] is True, hp


async def test_page_survives_across_separate_mcp_sessions():
    """The browser belongs to the process, not to an MCP connection."""
    async with mcp_session() as session:
        await call(session, "navigate", {"url": PAGE_ONE})
        await call(session, "evaluate",
                   {"script": "() => { window.__agentbox_cross_session = 'kept'; return true; }"})

    # Entirely new HTTP connection and MCP session.
    async with mcp_session() as session:
        probe = await call(session, "evaluate", {"script": "() => window.__agentbox_cross_session"})
        assert probe["result"] == '"kept"', (
            f"page did not survive a new MCP session — got {probe['result']!r}"
        )


async def test_coordinate_and_scroll_tools():
    """The by-coordinate tools operate on that same live page."""
    async with mcp_session() as session:
        nav = await call(session, "navigate", {"url": PAGE_ONE})
        assert nav["success"], nav

        scrolled = await call(session, "scroll", {"delta_y": 200})
        assert scrolled["success"], scrolled

        clicked = await call(session, "click_at_percent", {"x_percent": 50, "y_percent": 50})
        assert clicked["success"], clicked
        assert clicked["x"] == 640 and clicked["y"] == 360, clicked

        key = await call(session, "press_key", {"key": "End"})
        assert key["success"], key


async def test_type_at_focuses_then_types():
    """type_at with coordinates clicks to focus, then types into that field."""
    page = (
        "data:text/html,"
        "<input id=box style='position:absolute;left:0;top:0;width:1280px;height:720px'>"
    )
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": page}))["success"]

        typed = await call(session, "type_at",
                           {"text": "hello agentbox", "x_percent": 50, "y_percent": 50})
        assert typed["success"], typed

        value = await call(session, "evaluate",
                           {"script": "() => document.getElementById('box').value"})
        assert value["result"] == '"hello agentbox"', value


async def test_click_by_selector_navigates():
    page = "data:text/html,<a id=go href='https://example.com/'>go</a>"
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": page}))["success"]

        clicked = await call(session, "click", {"selector": "#go"})
        assert clicked["success"], clicked
        assert "example.com" in clicked["url"], clicked
        assert clicked["title"] == "Example Domain", clicked


async def test_history_reload_and_forward():
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]
        assert (await call(session, "navigate", {"url": PAGE_TWO}))["success"]

        back = await call(session, "history", {"action": "back"})
        assert back["success"] and "agentbox=second" not in back["url"], back

        forward = await call(session, "history", {"action": "forward"})
        assert forward["success"], forward
        assert "agentbox=second" in forward["url"], forward

        reload = await call(session, "history", {"action": "reload"})
        assert reload["success"], reload
        assert "agentbox=second" in reload["url"], reload
