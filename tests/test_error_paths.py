#!/usr/bin/env python3
"""
Error paths.

Every tool is expected to fail *soft*: a structured
{"success": false, "error": "..."} payload, never an MCP protocol error
and never a dead browser. The final test in this file is the one that
matters most — after every kind of failure above it, the same page is
still fully usable.
"""

import pytest

from conftest import PAGE_ONE, call, mcp_session


async def test_navigate_unresolvable_host():
    async with mcp_session() as session:
        r = await call(session, "navigate",
                       {"url": "https://this-host-does-not-exist-agentbox.invalid/"})
        assert r["success"] is False, r
        assert "ERR_NAME_NOT_RESOLVED" in r["error"], r


async def test_navigate_malformed_url():
    async with mcp_session() as session:
        r = await call(session, "navigate", {"url": "not-a-url"})
        assert r["success"] is False, r
        assert "invalid URL" in r["error"], r


async def test_navigate_connection_refused():
    """Nothing is listening on this port inside the container."""
    async with mcp_session() as session:
        r = await call(session, "navigate", {"url": "http://127.0.0.1:8123/"})
        assert r["success"] is False, r
        assert "ERR_" in r["error"], r


async def test_click_missing_selector_times_out_cleanly():
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]

        r = await call(session, "click", {"selector": "#no-such-element-anywhere"})
        assert r["success"] is False, r
        assert "Timeout" in r["error"], r


async def test_click_empty_selector_is_rejected_without_touching_the_page():
    async with mcp_session() as session:
        r = await call(session, "click", {"selector": ""})
        assert r["success"] is False, r
        assert r["error"] == "selector is required", r


async def test_get_text_missing_selector_times_out_cleanly():
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]

        r = await call(session, "get_text", {"selector": "#no-such-element-anywhere"})
        assert r["success"] is False, r
        assert "Timeout" in r["error"], r


async def test_evaluate_js_that_throws():
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]

        r = await call(session, "evaluate",
                       {"script": "() => { throw new Error('boom-from-test') }"})
        assert r["success"] is False, r
        assert "boom-from-test" in r["error"], r


async def test_evaluate_reference_error():
    async with mcp_session() as session:
        r = await call(session, "evaluate", {"script": "no_such_function_xyz()"})
        assert r["success"] is False, r
        assert "ReferenceError" in r["error"], r


async def test_evaluate_empty_script_is_rejected():
    async with mcp_session() as session:
        r = await call(session, "evaluate", {"script": ""})
        assert r["success"] is False, r
        assert r["error"] == "script is required", r


async def test_unknown_history_action():
    async with mcp_session() as session:
        r = await call(session, "history", {"action": "sideways"})
        assert r["success"] is False, r
        assert "Unknown action" in r["error"], r


async def test_unknown_key_name():
    async with mcp_session() as session:
        r = await call(session, "press_key", {"key": "NotARealKey"})
        assert r["success"] is False, r
        assert "Unknown key" in r["error"], r


async def test_error_text_is_truncated():
    """Errors are capped so a giant Playwright call log can't flood the client."""
    async with mcp_session() as session:
        r = await call(session, "evaluate",
                       {"script": "() => { throw new Error('x'.repeat(5000)) }"})
        assert r["success"] is False
        assert len(r["error"]) <= 300, len(r["error"])


@pytest.mark.parametrize("selector", ["", "   ", ">>>bad-selector", "div:nonsense-pseudo"])
async def test_bad_selectors_never_raise(selector):
    """Whatever the client sends, we answer with JSON rather than a stack trace."""
    async with mcp_session() as session:
        r = await call(session, "get_text", {"selector": selector})
        assert r["success"] is False, r
        assert isinstance(r["error"], str) and r["error"], r


async def test_browser_still_works_after_all_of_the_above():
    """The whole point: none of those failures killed the persistent page."""
    async with mcp_session() as session:
        nav = await call(session, "navigate", {"url": PAGE_ONE})
        assert nav["success"], nav
        assert nav["status"] == 200, nav

        text = await call(session, "get_text", {"selector": "body"})
        assert text["success"] and "Example Domain" in text["text"], text

        hp = await call(session, "health")
        assert hp["browser_started"] is True, hp
