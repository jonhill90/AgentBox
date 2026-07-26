#!/usr/bin/env python3
"""
AgentBox MCP Server

A browser-only MCP server: it gives an MCP client a real, persistent
Chromium page it can drive across many separate tool calls.

The browser internals (persistent background event loop + single
long-lived Playwright Page) are ported from Hill90's
services/agentbox/app/tools.py. Only the tool-registration glue is
different: this repo uses FastMCP's @mcp.tool() decorator instead of
Hill90's AgentRuntime/tool-registry abstraction.
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
mcp = FastMCP(
    "agentbox",
    host="0.0.0.0",
    port=8000
)


# Add custom health endpoint to underlying Starlette app
@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(request):
    """HTTP health endpoint for Docker healthcheck"""
    return JSONResponse({
        "status": "healthy",
        "service": "agentbox",
        "version": "1.0.0"
    })


# ── Persistent background event loop for Playwright browser ──────────
#
# The Playwright Page is bound to the asyncio event loop on which it was
# created. FastMCP serves each request on its own server loop, and a
# page created inside one request handler is not safely usable from the
# next. So we create a dedicated daemon thread running a forever-loop
# that owns the browser, and every tool dispatches into it via
# asyncio.run_coroutine_threadsafe(), which is thread-safe and
# loop-safe. This is the piece that makes the browser survive across
# separate tool calls; the page is never recreated per call.
_browser_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_browser_loop_thread = threading.Thread(
    target=_browser_loop.run_forever,
    name="agentbox-browser-loop",
    daemon=True,
)
_browser_loop_thread.start()


def _run_on_browser_loop_sync(coro, timeout: float = 15.0) -> object:
    """Dispatch a coroutine onto the persistent browser loop (thread-safe).

    Blocks the caller until the coroutine completes or timeout expires.
    Safe to call from any thread (MCP tool handlers, tests).
    """
    future = asyncio.run_coroutine_threadsafe(coro, _browser_loop)
    return future.result(timeout=timeout)


# ── Browser (Playwright chromium) ────────────────────────────────────
#
# All Playwright state lives on the persistent background loop
# `_browser_loop` started at module import. Any caller must dispatch via
# `_run_on_browser_loop_sync()`, which uses
# asyncio.run_coroutine_threadsafe to cross thread/loop boundaries.
_browser_context: object | None = None  # playwright BrowserContext
_browser_page: object | None = None     # playwright Page
_playwright_instance: object | None = None
_browser_last_screenshot: bytes | None = None  # cached PNG (populated on _browser_loop)
_browser_last_url: str | None = None           # URL captured alongside screenshot

MAX_TEXT_LENGTH = 4000
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/workspace/screenshots")


async def _ensure_browser_page_on_loop():
    """Create Playwright objects. MUST run on _browser_loop."""
    global _playwright_instance, _browser_context, _browser_page

    if _browser_page is not None:
        return _browser_page

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    from playwright.async_api import async_playwright

    _playwright_instance = await async_playwright().start()
    browser = await _playwright_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
    )
    _browser_context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="AgentBox/1.0 (Headless Chromium)",
    )
    _browser_page = await _browser_context.new_page()
    logger.info("Browser page created on persistent loop")
    return _browser_page


async def _capture_live_screenshot_on_loop() -> None:
    """Capture screenshot into module-level cache. MUST run on _browser_loop."""
    global _browser_last_screenshot, _browser_last_url
    if _browser_page is None:
        return
    try:
        _browser_last_screenshot = await _browser_page.screenshot(full_page=False)
        _browser_last_url = _browser_page.url
    except Exception:
        pass  # Non-blocking — stale screenshot is better than none


def _run_browser_op(coro_fn, timeout: float = 15.0) -> dict:
    """Run a browser coroutine on the persistent loop and return result dict.

    Ensures the browser page exists first, then dispatches the operation.
    Catches all exceptions and converts them to structured error dicts.
    """
    async def _wrapped():
        await _ensure_browser_page_on_loop()
        return await coro_fn(_browser_page)
    try:
        return _run_on_browser_loop_sync(_wrapped(), timeout=timeout)
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}


async def _browser_op(coro_fn, timeout: float = 15.0) -> str:
    """Async adapter used by the MCP tools.

    _run_browser_op blocks the calling thread while the browser loop
    works, so we push it to a worker thread rather than stalling the
    MCP server's own event loop. Returns a JSON string.
    """
    result = await asyncio.to_thread(_run_browser_op, coro_fn, timeout)
    return json.dumps(result)


def click_browser_at_percent(x_percent: float, y_percent: float, timeout: float = 10.0) -> dict:
    """Click the browser page at (x%, y%). Safe to call from any thread/loop."""
    async def _do_click(page):
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        x = int(viewport["width"] * x_percent / 100)
        y = int(viewport["height"] * y_percent / 100)
        await page.mouse.click(x, y)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass  # Not all clicks navigate
        await _capture_live_screenshot_on_loop()
        return {"success": True, "x": x, "y": y, "url": page.url}
    return _run_browser_op(_do_click, timeout)


def get_element_at_percent(x_percent: float, y_percent: float, timeout: float = 10.0) -> dict:
    """Identify the DOM element at (x%, y%) without clicking it.

    Returns element tag, id, classes, text, bounding box.
    """
    async def _get_element(page):
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        x = int(viewport["width"] * x_percent / 100)
        y = int(viewport["height"] * y_percent / 100)
        info = await page.evaluate(
            """([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return null;
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    classes: Array.from(el.classList),
                    text: (el.textContent || '').trim().slice(0, 100),
                    selector: el.id ? `#${el.id}` : el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).trim().split(/\\s+/).slice(0,2).join('.') : ''),
                    box: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
                    outerHTML: el.outerHTML.slice(0, 300),
                };
            }""",
            [x, y]
        )
        return {"success": True, "element": info, "url": page.url}
    return _run_browser_op(_get_element, timeout)


def navigate_browser(url: str, timeout: float = 35.0) -> dict:
    """Navigate the browser to a URL. Safe to call from any thread/loop."""
    async def _navigate(page):
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _capture_live_screenshot_on_loop()
        return {
            "success": True,
            "url": page.url,
            "status": resp.status if resp else None,
            "title": await page.title(),
        }
    return _run_browser_op(_navigate, timeout)


def type_in_browser(text: str, timeout: float = 10.0) -> dict:
    """Type text into the currently focused element."""
    async def _type(page):
        await page.keyboard.type(text, delay=30)
        await _capture_live_screenshot_on_loop()
        return {"success": True, "url": page.url}
    return _run_browser_op(_type, timeout)


def press_key_in_browser(key: str, timeout: float = 5.0) -> dict:
    """Press a keyboard key (Enter, Tab, Escape, Backspace, etc.)."""
    async def _press(page):
        await page.keyboard.press(key)
        await asyncio.sleep(0.15)
        await _capture_live_screenshot_on_loop()
        return {"success": True, "url": page.url}
    return _run_browser_op(_press, timeout)


def scroll_browser(delta_x: float = 0, delta_y: float = 0, timeout: float = 5.0) -> dict:
    """Scroll the browser page by (deltaX, deltaY) pixels."""
    async def _scroll(page):
        await page.mouse.wheel(delta_x, delta_y)
        await asyncio.sleep(0.15)  # Let scroll settle before screenshot
        await _capture_live_screenshot_on_loop()
        return {"success": True, "url": page.url}
    return _run_browser_op(_scroll, timeout)


def browser_history(action: str, timeout: float = 15.0) -> dict:
    """Navigate browser history: back, forward, or reload."""
    async def _nav(page):
        if action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=10000)
        elif action == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=10000)
        else:
            return {"success": False, "error": f"Unknown action: {action}"}
        await _capture_live_screenshot_on_loop()
        return {"success": True, "url": page.url, "title": await page.title()}
    return _run_browser_op(_nav, timeout)


# ── MCP tool surface (SPEC §5 — nothing beyond this list) ────────────


@mcp.tool()
async def navigate(url: str) -> str:
    """Navigate the persistent browser page to a URL.

    Args:
        url: Absolute URL to open (e.g. "https://example.com")

    Returns:
        JSON string with success, url, status, title
    """
    logger.info(f"navigate: {url}")
    return json.dumps(await asyncio.to_thread(navigate_browser, url))


@mcp.tool()
async def screenshot(full_page: bool = True) -> str:
    """Screenshot the current page.

    Args:
        full_page: Capture the whole scrollable page (default) or just the viewport

    Returns:
        JSON string with success, url, path (in-container file) and
        image_base64 (PNG bytes, base64-encoded)
    """
    filename = f"screenshot-{int(time.time() * 1000)}.png"
    path = f"{SCREENSHOT_DIR}/{filename}"

    async def _screenshot(page):
        png = await page.screenshot(path=path, full_page=full_page)
        await _capture_live_screenshot_on_loop()
        return {
            "success": True,
            "path": path,
            "url": page.url,
            "bytes": len(png),
            "image_base64": base64.b64encode(png).decode("ascii"),
        }
    return await _browser_op(_screenshot, timeout=30.0)


@mcp.tool()
async def click(selector: str) -> str:
    """Click an element by CSS selector.

    Args:
        selector: CSS selector of the element to click

    Returns:
        JSON string with success, url, title
    """
    if not selector:
        return json.dumps({"success": False, "error": "selector is required"})

    async def _click_selector(page):
        await page.click(selector, timeout=10000)
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        await _capture_live_screenshot_on_loop()
        return {"success": True, "url": page.url, "title": await page.title()}
    return await _browser_op(_click_selector, timeout=30.0)


@mcp.tool()
async def get_text(selector: str = "body") -> str:
    """Read the visible text of an element (defaults to the whole body).

    Args:
        selector: CSS selector to read text from

    Returns:
        JSON string with success and text (truncated at 4000 chars)
    """
    async def _get_text(page):
        element = page.locator(selector).first
        text = await element.inner_text(timeout=10000)
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + f"\n...(truncated, {len(text)} total chars)"
        return {"success": True, "text": text}
    return await _browser_op(_get_text, timeout=20.0)


@mcp.tool()
async def evaluate(script: str) -> str:
    """Evaluate JavaScript in the page and return its result.

    Args:
        script: JavaScript expression or function body to evaluate

    Returns:
        JSON string with success and result (JSON-encoded, truncated at 4000 chars)
    """
    if not script:
        return json.dumps({"success": False, "error": "script is required"})

    async def _evaluate(page):
        result = await page.evaluate(script)
        text = json.dumps(result, default=str)
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH] + "...(truncated)"
        await _capture_live_screenshot_on_loop()
        return {"success": True, "result": text}
    return await _browser_op(_evaluate, timeout=20.0)


@mcp.tool()
async def click_at_percent(x_percent: float, y_percent: float) -> str:
    """Click at a viewport coordinate expressed as percentages.

    Args:
        x_percent: Horizontal position, 0-100
        y_percent: Vertical position, 0-100

    Returns:
        JSON string with success, resolved x/y pixels, url
    """
    return json.dumps(await asyncio.to_thread(click_browser_at_percent, x_percent, y_percent))


@mcp.tool()
async def type_at(text: str, x_percent: float | None = None, y_percent: float | None = None) -> str:
    """Type text, optionally clicking a percentage coordinate first to focus it.

    Args:
        text: Text to type into the focused element
        x_percent: Optional horizontal position to click first, 0-100
        y_percent: Optional vertical position to click first, 0-100

    Returns:
        JSON string with success and url
    """
    if x_percent is not None and y_percent is not None:
        clicked = await asyncio.to_thread(click_browser_at_percent, x_percent, y_percent)
        if not clicked.get("success"):
            return json.dumps(clicked)
    return json.dumps(await asyncio.to_thread(type_in_browser, text))


@mcp.tool()
async def press_key(key: str) -> str:
    """Press a keyboard key (Enter, Tab, Escape, Backspace, ArrowDown, ...).

    Args:
        key: Playwright key name

    Returns:
        JSON string with success and url
    """
    return json.dumps(await asyncio.to_thread(press_key_in_browser, key))


@mcp.tool()
async def scroll(delta_x: float = 0, delta_y: float = 0) -> str:
    """Scroll the page by a pixel delta.

    Args:
        delta_x: Horizontal scroll in pixels
        delta_y: Vertical scroll in pixels

    Returns:
        JSON string with success and url
    """
    return json.dumps(await asyncio.to_thread(scroll_browser, delta_x, delta_y))


@mcp.tool()
async def history(action: str) -> str:
    """Move through the page's session history.

    Args:
        action: "back" | "forward" | "reload"

    Returns:
        JSON string with success, url, title
    """
    return json.dumps(await asyncio.to_thread(browser_history, action))


@mcp.tool()
async def health() -> str:
    """Health check for Docker monitoring."""
    return json.dumps({
        "status": "healthy",
        "service": "agentbox",
        "version": "1.0.0",
        "browser_started": _browser_page is not None,
    })


if __name__ == "__main__":
    logger.info("Starting AgentBox MCP server...")
    logger.info("   Mode: Streamable HTTP")
    logger.info("   URL: http://0.0.0.0:8000/mcp")

    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        logger.info("AgentBox MCP server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
