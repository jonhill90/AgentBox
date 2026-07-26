#!/usr/bin/env python3
"""
Take-control viewer UI and its REST surface (SPEC.md §6, §7).

The REST routes are thin wrappers over the same internal functions the
MCP tools call, so the tests that matter most here are the cross-surface
ones: drive the page over REST, read it back over MCP, and confirm both
were talking to the one persistent page.

One test here restarts the shared container; the tests after it
re-navigate rather than assuming prior state.
"""

import base64

from conftest import (
    PAGE_ONE,
    PAGE_TWO,
    call,
    health_ok,
    mcp_session,
    requires_docker_introspection,
    rest_get,
    rest_post,
    restart_container,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# A full-viewport input, so a click anywhere on the image focuses it.
TYPING_PAGE = (
    "data:text/html,"
    "<input id=box style='position:absolute;left:0;top:0;width:1280px;height:720px'>"
)


async def test_ui_page_is_served():
    status, html = await rest_get("/ui")
    assert status == 200, html
    assert "<title>AgentBox" in html
    # The behaviours SPEC §6 requires of the frontend.
    for marker in ["Take Control", "/api/screenshot", "/api/browser/click",
                   "/api/browser/scroll", "/api/browser/keypress",
                   "/api/browser/type", "/api/browser/history",
                   "/api/browser/navigate"]:
        assert marker in html, f"UI page never references {marker}"
    assert "POLL_MS = 2000" in html, "screenshot polling is not ~2s"


async def test_navigate_then_screenshot_returns_a_real_png():
    """SPEC §7: the persistence proof, through REST instead of MCP."""
    status, nav = await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    assert status == 200 and nav["success"], nav
    assert nav["status"] == 200 and nav["title"] == "Example Domain", nav

    status, shot = await rest_get("/api/screenshot")
    assert status == 200, shot
    png = base64.b64decode(shot["screenshot"])
    assert png.startswith(PNG_MAGIC), "not a PNG"
    assert len(png) > 1000, f"screenshot suspiciously small: {len(png)} bytes"
    assert shot["url"].startswith("https://example.com"), shot["url"]


async def test_screenshot_reflects_later_navigation():
    """Polling shows the live page, not a stale first capture."""
    await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    _, first = await rest_get("/api/screenshot")

    await rest_post("/api/browser/navigate", {"url": TYPING_PAGE})
    _, second = await rest_get("/api/screenshot")

    assert second["url"] != first["url"], "screenshot URL did not follow the navigation"
    assert second["screenshot"] != first["screenshot"], "screenshot image never changed"
    assert base64.b64decode(second["screenshot"]).startswith(PNG_MAGIC)


async def test_history_buttons_over_rest():
    await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    await rest_post("/api/browser/navigate", {"url": PAGE_TWO})

    status, back = await rest_post("/api/browser/history", {"action": "back"})
    assert status == 200 and back["success"], back
    assert "agentbox=second" not in back["url"], back

    _, forward = await rest_post("/api/browser/history", {"action": "forward"})
    assert forward["success"] and "agentbox=second" in forward["url"], forward

    _, reload = await rest_post("/api/browser/history", {"action": "reload"})
    assert reload["success"] and "agentbox=second" in reload["url"], reload


async def test_take_control_click_and_type_reach_the_real_page():
    """What the Take Control toggle does: click to focus, then keystrokes."""
    await rest_post("/api/browser/navigate", {"url": TYPING_PAGE})

    status, clicked = await rest_post("/api/browser/click", {"x_percent": 50, "y_percent": 50})
    assert status == 200 and clicked["success"], clicked
    assert clicked["x"] == 640 and clicked["y"] == 360, clicked

    for ch in "hi":
        status, typed = await rest_post("/api/browser/type", {"text": ch})
        assert status == 200 and typed["success"], typed

    # Read it back over MCP — a different surface entirely.
    async with mcp_session() as session:
        value = await call(session, "evaluate",
                           {"script": "() => document.getElementById('box').value"})
        assert value["result"] == '"hi"', (
            f"REST keystrokes did not land on the page MCP sees: {value['result']!r}"
        )


async def test_keypress_over_rest_reaches_the_real_page():
    page = ("data:text/html,<input id=box autofocus "
            "style='position:absolute;left:0;top:0;width:1280px;height:720px'>")
    await rest_post("/api/browser/navigate", {"url": page})
    await rest_post("/api/browser/click", {"x_percent": 50, "y_percent": 50})
    await rest_post("/api/browser/type", {"text": "ab"})

    status, pressed = await rest_post("/api/browser/keypress", {"key": "Backspace"})
    assert status == 200 and pressed["success"], pressed

    async with mcp_session() as session:
        value = await call(session, "evaluate",
                           {"script": "() => document.getElementById('box').value"})
        assert value["result"] == '"a"', value


async def test_scroll_over_rest_moves_the_real_page():
    page = "data:text/html,<body style='height:5000px'>tall</body>"
    await rest_post("/api/browser/navigate", {"url": page})

    status, scrolled = await rest_post("/api/browser/scroll", {"delta_x": 0, "delta_y": 400})
    assert status == 200 and scrolled["success"], scrolled

    async with mcp_session() as session:
        pos = await call(session, "evaluate", {"script": "() => Math.round(window.scrollY)"})
        assert int(pos["result"]) > 0, f"page did not scroll: {pos['result']}"


async def test_rest_and_mcp_drive_the_same_persistent_page():
    """Navigate over REST, read over MCP — one page, two surfaces."""
    await rest_post("/api/browser/navigate", {"url": PAGE_ONE})

    async with mcp_session() as session:
        text = await call(session, "get_text", {"selector": "body"})
        assert text["success"] and "Example Domain" in text["text"], text

        # ...and the reverse direction.
        await call(session, "navigate", {"url": TYPING_PAGE})

    _, shot = await rest_get("/api/screenshot")
    assert shot["url"].startswith("data:text/html"), shot["url"]


async def test_rest_reports_browser_errors_without_dying():
    status, nav = await rest_post(
        "/api/browser/navigate",
        {"url": "https://this-host-does-not-exist-agentbox.invalid/"},
    )
    assert status == 200, nav
    assert nav["success"] is False and "ERR_NAME_NOT_RESOLVED" in nav["error"], nav

    status, hist = await rest_post("/api/browser/history", {"action": "sideways"})
    assert status == 200 and hist["success"] is False, hist
    assert "Unknown action" in hist["error"], hist

    # Still alive — and a good URL typed straight after a bad one works.
    # Without the retry in navigate_browser this fails: the error page
    # commits mid-flight and Chromium reports the new navigation as
    # "interrupted by another navigation to chrome-error://chromewebdata/".
    status, nav = await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    assert nav["success"], nav
    assert nav["status"] == 200, nav


async def test_missing_parameters_are_rejected():
    for path, expected in [
        ("/api/browser/navigate", "url is required"),
        ("/api/browser/click", "x_percent and y_percent"),
        ("/api/browser/keypress", "key is required"),
        ("/api/browser/type", "text is required"),
        ("/api/browser/history", "action is required"),
    ]:
        status, body = await rest_post(path, {})
        assert status == 400, f"{path} -> {status} {body}"
        assert body["success"] is False and expected in body["error"], body


async def test_scroll_defaults_to_zero_when_body_is_empty():
    """The one POST route with no required fields still answers cleanly."""
    await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    status, body = await rest_post("/api/browser/scroll", {})
    assert status == 200 and body["success"], body


@requires_docker_introspection
async def test_screenshot_404s_before_the_browser_exists():
    """Mirrors Hill90's "Browser not active" state.

    Polling this route must never launch a browser by itself, or simply
    opening the viewer would start Chromium behind the operator's back.
    """
    restart_container()
    assert health_ok()

    status, body = await rest_get("/api/screenshot")
    assert status == 404, body
    assert body["error"] == "Browser not active", body

    # Poll a few more times: still no browser.
    for _ in range(3):
        status, _body = await rest_get("/api/screenshot")
        assert status == 404

    async with mcp_session() as session:
        hp = await call(session, "health")
        assert hp["browser_started"] is False, "polling /api/screenshot launched a browser"

    # And once something does navigate, the route starts serving.
    _, nav = await rest_post("/api/browser/navigate", {"url": PAGE_ONE})
    assert nav["success"], nav

    status, shot = await rest_get("/api/screenshot")
    assert status == 200, shot
    assert base64.b64decode(shot["screenshot"]).startswith(PNG_MAGIC)


# ── Describe element-picker mode (SPEC §11) ──────────────────────────

DESCRIBE_PAGE = (
    "data:text/html,<div id=target class='a b c' "
    "style='position:absolute;left:0;top:0;width:1280px;height:720px'>hello describe</div>"
)


async def test_element_route_describes_what_is_under_the_point():
    await rest_post("/api/browser/navigate", {"url": DESCRIBE_PAGE})

    status, body = await rest_post("/api/browser/element", {"x_percent": 50, "y_percent": 50})
    assert status == 200 and body["success"], body

    el = body["element"]
    assert el["tag"] == "div", el
    assert el["id"] == "target", el
    assert el["classes"] == ["a", "b", "c"], el
    assert "hello describe" in el["text"], el
    assert el["selector"] == "#target", el
    assert el["box"]["w"] > 0 and el["box"]["h"] > 0, el
    assert el["outerHTML"].startswith("<div"), el


async def test_element_route_does_not_click_the_page():
    """Describe inspects; it must not fire a real click."""
    page = ("data:text/html,<button id=b onclick=\"window.__clicked=1\" "
            "style='position:absolute;left:0;top:0;width:1280px;height:720px'>b</button>")
    await rest_post("/api/browser/navigate", {"url": page})

    await rest_post("/api/browser/element", {"x_percent": 50, "y_percent": 50})

    async with mcp_session() as session:
        probe = await call(session, "evaluate", {"script": "() => window.__clicked || 0"})
        assert probe["result"] == "0", f"Describe fired a real click: {probe}"

    # ...whereas the click route does.
    await rest_post("/api/browser/click", {"x_percent": 50, "y_percent": 50})
    async with mcp_session() as session:
        probe = await call(session, "evaluate", {"script": "() => window.__clicked || 0"})
        assert probe["result"] == "1", probe


async def test_element_route_reports_empty_space_without_failing():
    await rest_post("/api/browser/navigate", {"url": "data:text/html,<p>tiny</p>"})
    status, body = await rest_post("/api/browser/element", {"x_percent": 99, "y_percent": 99})
    assert status == 200 and body["success"], body
    assert body["element"] is None or isinstance(body["element"], dict), body


async def test_element_route_requires_coordinates():
    status, body = await rest_post("/api/browser/element", {})
    assert status == 400, body
    assert body["success"] is False and "x_percent" in body["error"], body


async def test_ui_page_wires_up_describe_mode():
    status, html = await rest_get("/ui")
    assert status == 200
    assert "/api/browser/element" in html, "UI never calls the element route"
    assert 'id="describe"' in html, "no Describe toggle in the UI"
    # Describe is only offered while Take Control is on (Hill90's behaviour).
    assert 'els.describe.classList.toggle("shown", on)' in html
    assert "if (!on) setDescribe(false)" in html


# ── terminal panel assets and capability probe (SPEC §15.5) ──────────

async def test_capability_probe_reports_terminal_state():
    """The viewer only offers the terminal button when the server has it on."""
    status, body = await rest_get("/api/auth-required")
    assert status == 200, body
    assert "terminal_enabled" in body, body
    # Reflects THIS container's configuration, not the code default — the
    # code default is asserted in test_feature_toggle.py against a container
    # started without the variable at all.
    assert isinstance(body["terminal_enabled"], bool), body


async def test_vendored_xterm_assets_are_served():
    """Vendored, not CDN: /ui must work with no outbound network."""
    for name, ctype, min_size in [
        ("xterm.js", "javascript", 100_000),
        ("xterm.css", "css", 1_000),
        ("xterm-addon-fit.js", "javascript", 500),
    ]:
        status, body = await rest_get(f"/vendor/{name}")
        assert status == 200, f"/vendor/{name} -> {status}"
        assert len(body) > min_size, f"/vendor/{name} is only {len(body)} bytes"


async def test_vendor_route_rejects_unknown_and_traversal():
    for path in ["/vendor/nope.js", "/vendor/..%2f..%2fetc%2fpasswd",
                 "/vendor/mcp_server.py", "/vendor/xterm.js.map"]:
        status, _body = await rest_get(path)
        assert status == 404, f"{path} -> {status}"


async def test_ui_page_wires_up_the_terminal_view():
    status, html = await rest_get("/ui")
    assert status == 200
    for marker in ['id="termview"', 'id="tab-terminal"', "/vendor/xterm.js",
                   "/vendor/xterm.css",                    "new Terminal(", "FitAddon.FitAddon", '"resize"', "termWsUrl"]:
        assert marker in html, f"UI page is missing {marker}"
    # The terminal tab only appears when the server reports it on.
    assert "tabEls.terminal.hidden = !termEnabled" in html


async def test_ui_uses_tabs_rather_than_a_split_pane():
    """Each view owns the full pane; the terminal must not squash the browser."""
    status, html = await rest_get("/ui")
    assert 'id="tabs"' in html and 'id="view-browser"' in html
    assert "function activateTab" in html
    # The old fixed-height split is gone.
    assert "flex: 0 0 40%" not in html, "terminal is still taking a fixed slice"
    assert 'id="termpanel"' not in html, "old split-panel markup left behind"


async def test_terminal_uses_the_nerd_font_by_name():
    """Fonts are not vendored — FiraCode Nerd Font is installed in the
    image and on the operator's machine, exactly as hill90-app does it.
    A vendored subset previously shadowed the real font and rendered the
    powerline separators blank."""
    status, html = await rest_get("/ui")
    assert status == 200
    assert "'FiraCode Nerd Font'" in html, "UI does not ask for the Nerd Font"
    assert "nerd-symbols" not in html, "the broken vendored subset is back"

    status, _ = await rest_get("/vendor/nerd-symbols.woff2")
    assert status == 404, "a vendored font is being served again"
