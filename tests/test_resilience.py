#!/usr/bin/env python3
"""
Resource cleanup, load, and restart behaviour.

These tests use `docker top` / `docker compose logs` against the local
container, so they skip automatically when AGENTBOX_URL points somewhere
else. They run last (file name sorts last) because the final one
restarts the container.
"""

import asyncio
import base64

from conftest import (
    PAGE_ONE,
    browser_creation_count,
    call,
    chromium_process_count,
    health_ok,
    mcp_session,
    requires_docker_introspection,
    restart_container,
)


async def test_rapid_back_to_back_calls_do_not_deadlock():
    """120 sequential calls across every tool, hard on the heels of each other."""
    async with mcp_session() as session:
        failures = []
        for i in range(20):
            for name, args in [
                ("navigate", {"url": f"https://example.com/?i={i}"}),
                ("get_text", {"selector": "body"}),
                ("evaluate", {"script": f"() => {i}"}),
                ("scroll", {"delta_y": 50}),
                ("press_key", {"key": "End"}),
                ("screenshot", {"full_page": False}),
            ]:
                result = await asyncio.wait_for(call(session, name, args), timeout=60)
                if not result.get("success"):
                    failures.append((i, name, result.get("error", "")[:120]))
        assert not failures, f"{len(failures)} failed calls: {failures[:5]}"


@requires_docker_introspection
async def test_repeated_navigation_does_not_leak_browsers():
    """The page is reused, not rebuilt — process count stays flat."""
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]

        creations_before = browser_creation_count()
        procs_before = chromium_process_count()
        assert procs_before > 0, "expected a live Chromium after navigating"

        for i in range(25):
            r = await call(session, "navigate", {"url": f"https://example.com/?leak={i}"})
            assert r["success"], r

        # Give any orphaned process a moment to show up before we count.
        await asyncio.sleep(2)

        assert browser_creation_count() == creations_before, (
            "the browser page was rebuilt during repeated navigation"
        )
        assert chromium_process_count() == procs_before, (
            f"Chromium process count drifted {procs_before} -> {chromium_process_count()}; "
            "pages or contexts are leaking"
        )


@requires_docker_introspection
async def test_screenshot_load_does_not_leak_processes():
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]
        procs_before = chromium_process_count()

        for _ in range(15):
            shot = await call(session, "screenshot", {"full_page": True})
            assert shot["success"], shot
            assert base64.b64decode(shot["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")

        await asyncio.sleep(2)
        assert chromium_process_count() == procs_before, (
            f"Chromium process count drifted {procs_before} -> {chromium_process_count()}"
        )


@requires_docker_introspection
async def test_concurrent_cold_start_launches_exactly_one_browser():
    """Regression: concurrent first calls used to each launch their own browser.

    _ensure_browser_page_on_loop awaits several times while launching, so
    interleaved tasks all saw _browser_page as None, launched competing
    Playwright instances, and clobbered each other's globals — most calls
    then failed with "Target page, context or browser has been closed".
    An asyncio.Lock plus a re-check now guards that window.
    """
    restart_container()  # cold: no browser exists yet
    creations_before = browser_creation_count()

    async def cold_navigate():
        async with mcp_session() as session:
            return await call(session, "navigate", {"url": PAGE_ONE})

    results = await asyncio.gather(*(cold_navigate() for _ in range(8)),
                                   return_exceptions=True)

    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"concurrent cold start raised: {raised}"

    # Exactly one browser was built, no matter how many callers raced.
    assert browser_creation_count() - creations_before == 1, (
        "concurrent cold start built more than one browser"
    )

    # Nobody hit a torn-down browser. (Losing a navigation race is fine and
    # expected — one shared page, eight simultaneous goto calls, so Chromium
    # aborts the ones that get superseded. A dead browser is not fine.)
    for r in results:
        if not r["success"]:
            assert "has been closed" not in r["error"], f"raced onto a torn-down browser: {r}"
            assert "ERR_ABORTED" in r["error"], f"unexpected cold-start failure: {r}"
    assert any(r["success"] for r in results), f"no cold-start navigation succeeded: {results}"

    # And the page is fully usable afterwards.
    async with mcp_session() as session:
        after = await call(session, "navigate", {"url": PAGE_ONE})
        assert after["success"] and after["status"] == 200, after
        assert chromium_process_count() > 0


@requires_docker_introspection
async def test_container_restart_leaves_server_functional():
    """docker compose restart: server comes back and rebuilds the browser."""
    async with mcp_session() as session:
        assert (await call(session, "navigate", {"url": PAGE_ONE}))["success"]
        await call(session, "evaluate",
                   {"script": "() => { window.__agentbox_pre_restart = 1; return true; }"})

    restart_container()
    assert health_ok()

    async with mcp_session() as session:
        # Fresh process: the browser has not been built yet.
        hp = await call(session, "health")
        assert hp["status"] == "healthy", hp
        assert hp["browser_started"] is False, hp

        # ...and it builds cleanly on demand.
        nav = await call(session, "navigate", {"url": PAGE_ONE})
        assert nav["success"] and nav["status"] == 200, nav

        hp = await call(session, "health")
        assert hp["browser_started"] is True, hp

        # State from before the restart is gone, as it should be.
        probe = await call(session, "evaluate", {"script": "() => window.__agentbox_pre_restart"})
        assert probe["result"] == "null", probe

        shot = await call(session, "screenshot", {"full_page": False})
        assert shot["success"], shot
        assert base64.b64decode(shot["image_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
