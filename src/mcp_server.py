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
import functools
import json
import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.responses import FileResponse, HTMLResponse, JSONResponse

import auth

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ── Feature toggle framework (SPEC §10) ──────────────────────────────
#
# Read ONCE at startup. When a flag is off the corresponding tools are
# never registered with FastMCP — they are absent from the MCP surface,
# not present-but-refusing. That distinction is the whole point: a tool
# that exists and returns "disabled" is still an attack surface and
# still shows up in list_tools().
#
# Default is on, for local docker-compose dev. docker-compose.yml sets
# it explicitly anyway, so the "on for local dev" fact is visible in the
# deployment config rather than buried here (SPEC §10). Any future
# non-local profile must default it off, as its own reviewed decision.

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


JUMPBOX_TOOLS_ENABLED = _env_flag("AGENTBOX_ENABLE_JUMPBOX_TOOLS")

# The terminal gets its OWN flag (SPEC §14 item 4): a PTY is a
# categorically different risk from a path-scoped file read, and sharing
# a flag would make "filesystem on, PTY off" inexpressible. It is also
# the one toggle that defaults OFF, including for local dev — every
# other capability here is a structured tool; this one is a shell.
TERMINAL_ENABLED = _env_flag("AGENTBOX_ENABLE_TERMINAL", default="false")


# ── Origin / Host validation (DNS rebinding) ─────────────────────────
#
# The MCP transports spec: "Servers MUST validate the Origin header on all
# incoming connections to prevent DNS rebinding attacks", answering 403 on
# a mismatch. RFC 6455 §10.2 asks the same of WebSocket handshakes.
#
# Host is checked too, and that is the part people miss. Binding to
# loopback is NOT by itself a defence: an attacker's page can resolve a
# hostname they control to 127.0.0.1 and drive this server through your
# browser, which is exactly CVE-2015-8400 against Shellinabox. Origin
# alone does not stop it, because the attacker's page has its own origin
# and simply omits or sets it. Pinning Host to names we expect does.
#
# Requests with NO Origin header are allowed: curl, the MCP client and
# every non-browser caller omit it, and browsers always send one on
# cross-origin requests. Absence is therefore not the attack shape.

_DEFAULT_HOSTS = "localhost,127.0.0.1,[::1],0.0.0.0"


def _split_env(name: str, default: str) -> set[str]:
    return {v.strip().lower() for v in os.environ.get(name, default).split(",") if v.strip()}


ALLOWED_HOSTS = _split_env("AGENTBOX_ALLOWED_HOSTS", _DEFAULT_HOSTS)
ALLOWED_ORIGINS = _split_env("AGENTBOX_ALLOWED_ORIGINS", "")


def _host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return True  # HTTP/1.0 and some tooling omit it entirely.
    if "*" in ALLOWED_HOSTS:
        return True
    host = host_header.strip().lower()
    # Strip the port: any port on an allowed name is fine, since the port
    # is our own and the rebinding risk is in the NAME.
    if host.startswith("["):                       # [::1]:8054
        host = host.split("]")[0] + "]"
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in ALLOWED_HOSTS


def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True  # Non-browser client; see note above.
    if "*" in ALLOWED_ORIGINS:
        return True
    origin = origin.strip().lower()
    if origin in ALLOWED_ORIGINS:
        return True
    # Default policy: an Origin is acceptable when its HOST is one we
    # serve — i.e. the page came from this server. Explicit allowlist, no
    # substring matching (OWASP: "Use an allowlist, not a denylist").
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return _host_allowed(parsed.netloc)


def request_origin_ok(headers) -> bool:
    """True if this request may proceed. Used by HTTP and WebSocket alike."""
    return _host_allowed(headers.get("host")) and _origin_allowed(headers.get("origin"))


class RebindingGuard:
    """ASGI middleware applying the checks above to every http/websocket scope."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if not request_origin_ok(headers):
                logger.warning(
                    "Blocked request: host=%r origin=%r (DNS-rebinding guard)",
                    headers.get("host"), headers.get("origin"),
                )
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 4403})
                    return
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body",
                            "body": b'{"success": false, "status": 403, '
                                    b'"error": "Forbidden: Origin/Host not allowed"}'})
                return
        await self.app(scope, receive, send)


# ── Auth wiring (SPEC §12) ───────────────────────────────────────────
#
# `auth` holds the check itself; this is where it gets attached. When
# AGENTBOX_AUTH_TOKEN is unset these decorators are pass-throughs — the
# guard is not wired in at all, so behaviour is identical to a build
# without auth, which is what §12 requires of the default.
#
# Why registration-time decorators rather than FastMCP's own auth
# middleware: that hook is built for OAuth (TokenVerifier, scopes,
# issuer/resource metadata), which is explicitly Phase 2's problem, and
# `streamable_http_app()` rebuilds the Starlette app on every call, so
# there is no cached app to attach plain ASGI middleware to without
# giving up `mcp.run(transport="streamable-http")` — the shape §3 says
# to keep. §12's stated fallback is "a shared helper called at the top
# of each tool"; wrapping at registration is that, minus sixteen chances
# to forget it on a new tool.

def _tool(*args, **kwargs):
    """`@mcp.tool()`, plus the Bearer check when auth is enabled."""
    def decorator(fn):
        if auth.auth_enabled():
            fn = _guard_tool(fn)
        return mcp.tool(*args, **kwargs)(fn)
    return decorator


def _guard_tool(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        if not auth.mcp_request_authorized():
            logger.warning(f"Unauthorized MCP tool call: {fn.__name__}")
            return auth.UNAUTHORIZED_TOOL_RESULT
        return await fn(*args, **kwargs)
    return wrapper


def _api_route(path: str, methods: list[str]):
    """`@mcp.custom_route()` for /api/* routes, plus the Bearer check."""
    def decorator(fn):
        if auth.auth_enabled():
            fn = _guard_route(fn)
        return mcp.custom_route(path, methods=methods)(fn)
    return decorator


def _guard_route(fn):
    @functools.wraps(fn)
    async def wrapper(request):
        if not auth.check_bearer(request.headers.get("authorization")):
            logger.warning(f"Unauthorized REST call: {request.url.path}")
            return JSONResponse(auth.UNAUTHORIZED_BODY, status_code=401)
        return await fn(request)
    return wrapper


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

# Guards first-time browser creation. _browser_loop is single-threaded, but
# _ensure_browser_page_on_loop() awaits several times while launching, and
# concurrent tool calls interleave at those await points. Without this lock
# each of them sees _browser_page is None and launches its own Playwright +
# Chromium, and the last writer's globals clobber the rest — every racing
# call then fails with "Target page, context or browser has been closed".
# Only ever acquired from coroutines running on _browser_loop.
_browser_init_lock = asyncio.Lock()

MAX_TEXT_LENGTH = 4000
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/workspace/screenshots")


async def _ensure_browser_page_on_loop():
    """Create Playwright objects. MUST run on _browser_loop."""
    global _playwright_instance, _browser_context, _browser_page

    if _browser_page is not None:
        return _browser_page

    async with _browser_init_lock:
        # Re-check: another task may have finished the launch while we
        # were waiting for the lock.
        if _browser_page is not None:
            return _browser_page

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        from playwright.async_api import async_playwright

        instance = await async_playwright().start()
        browser = await instance.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="AgentBox/1.0 (Headless Chromium)",
        )
        page = await context.new_page()

        # Publish only once everything is built, so no other task can ever
        # observe a half-created browser.
        _playwright_instance, _browser_context, _browser_page = instance, context, page
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
        # A goto issued right after a failed one races the previous
        # navigation still settling: Chromium aborts ours and reports
        # "interrupted by another navigation to chrome-error://...".
        # Nothing is wrong with the URL, so let the pending navigation
        # land and try again. Hit by typing a bad URL then a good one in
        # the viewer's URL bar.
        attempts = 3
        for attempt in range(attempts):
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                break
            except Exception as exc:
                if "interrupted by another navigation" not in str(exc) or attempt == attempts - 1:
                    raise
                logger.info(f"navigate interrupted, retrying ({attempt + 1}/{attempts - 1}): {url}")
                await asyncio.sleep(0.4)
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


# ── Take-control viewer UI (SPEC §6) ─────────────────────────────────
#
# A browser cannot speak Streamable HTTP MCP, so the viewer gets its own
# plain HTTP surface. These routes are thin wrappers over the very same
# internal functions the MCP tools call — no second implementation of
# anything, and both surfaces drive the one persistent page. No auth:
# this is a local dev tool (PRD 1.3/1.4).

UI_HTML_PATH = Path(__file__).resolve().parent / "ui.html"


async def _json_body(request) -> dict:
    """Parse a JSON request body, tolerating an empty or malformed one."""
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _rest_result(result: dict) -> JSONResponse:
    """Wrap an internal function's result dict as a JSON response.

    Browser-level failures (bad URL, unknown key) are reported in the
    payload with success=false and HTTP 200, matching the MCP surface;
    only malformed requests get a 4xx.
    """
    return JSONResponse(result)


VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDOR_TYPES = {".js": "text/javascript", ".css": "text/css"}


@mcp.custom_route("/vendor/{name}", methods=["GET"])
async def vendor_asset(request):
    """Serve the vendored xterm.js assets for the terminal panel.

    Unauthenticated, like /ui itself: the page needs its scripts before
    it can prompt for a token. Only files that actually sit in src/vendor
    are served, and the name is reduced to a basename first, so a
    traversal like ../../etc/passwd cannot escape.
    """
    name = os.path.basename(request.path_params["name"])
    target = VENDOR_DIR / name
    if target.suffix not in VENDOR_TYPES or not target.is_file():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(target, media_type=VENDOR_TYPES[target.suffix])


@mcp.custom_route("/ui", methods=["GET"])
async def ui_page(request):
    """Serve the single-page take-control viewer."""
    try:
        return HTMLResponse(UI_HTML_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error(f"Cannot read UI page at {UI_HTML_PATH}: {exc}")
        return JSONResponse({"error": "UI page not found"}, status_code=500)


@mcp.custom_route("/api/auth-required", methods=["GET"])
async def api_auth_required(request):
    """Does this server need a Bearer token? (SPEC §12)

    Unauthenticated on purpose: the viewer has to be able to ask before
    it can know to prompt, and the answer reveals nothing a 401 would
    not already have told the caller.
    """
    return JSONResponse({
        "auth_required": auth.auth_enabled(),
        # Capability probe for the viewer: whether to offer the terminal
        # panel at all. Piggy-backs on this endpoint rather than adding a
        # second unauthenticated one; both answers are things a caller
        # would learn anyway from a 401 or a failed upgrade.
        "terminal_enabled": TERMINAL_ENABLED,
    })


@_api_route("/api/screenshot", methods=["GET"])
async def api_screenshot(request):
    """Current PNG of the live page, plus its URL.

    404s with "Browser not active" until something has actually created
    the page — polling this must never launch a browser on its own.
    """
    if _browser_page is None:
        return JSONResponse({"error": "Browser not active"}, status_code=404)

    def _refresh() -> None:
        # _capture_live_screenshot_on_loop no-ops when the page is gone.
        _run_on_browser_loop_sync(_capture_live_screenshot_on_loop(), timeout=15.0)

    try:
        await asyncio.to_thread(_refresh)
    except Exception as exc:
        logger.warning(f"screenshot refresh failed: {exc}")

    if _browser_last_screenshot is None:
        return JSONResponse({"error": "Browser not active"}, status_code=404)

    return JSONResponse({
        "screenshot": base64.b64encode(_browser_last_screenshot).decode("ascii"),
        "url": _browser_last_url,
        "bytes": len(_browser_last_screenshot),
    })


@_api_route("/api/browser/navigate", methods=["POST"])
async def api_navigate(request):
    body = await _json_body(request)
    url = body.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "url is required"}, status_code=400)
    return _rest_result(await asyncio.to_thread(navigate_browser, url))


@_api_route("/api/browser/click", methods=["POST"])
async def api_click(request):
    body = await _json_body(request)
    try:
        x_percent = float(body["x_percent"])
        y_percent = float(body["y_percent"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "x_percent and y_percent (numbers) are required"},
            status_code=400,
        )
    return _rest_result(await asyncio.to_thread(click_browser_at_percent, x_percent, y_percent))


@_api_route("/api/browser/scroll", methods=["POST"])
async def api_scroll(request):
    body = await _json_body(request)
    try:
        delta_x = float(body.get("delta_x", 0) or 0)
        delta_y = float(body.get("delta_y", 0) or 0)
    except (TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "delta_x and delta_y must be numbers"},
            status_code=400,
        )
    return _rest_result(await asyncio.to_thread(scroll_browser, delta_x, delta_y))


@_api_route("/api/browser/keypress", methods=["POST"])
async def api_keypress(request):
    body = await _json_body(request)
    key = body.get("key")
    if not key:
        return JSONResponse({"success": False, "error": "key is required"}, status_code=400)
    return _rest_result(await asyncio.to_thread(press_key_in_browser, key))


@_api_route("/api/browser/type", methods=["POST"])
async def api_type(request):
    body = await _json_body(request)
    text = body.get("text")
    if not text:
        return JSONResponse({"success": False, "error": "text is required"}, status_code=400)
    return _rest_result(await asyncio.to_thread(type_in_browser, text))


@_api_route("/api/browser/element", methods=["POST"])
async def api_element(request):
    """Describe mode (SPEC §11): identify the element at a coordinate.

    Reads DOM info from the page already being driven — it does not
    click, and touches nothing outside the browser. Core browser
    surface, same trust tier as screenshot; not gated by §10's toggle.
    """
    body = await _json_body(request)
    try:
        x_percent = float(body["x_percent"])
        y_percent = float(body["y_percent"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "x_percent and y_percent (numbers) are required"},
            status_code=400,
        )
    return _rest_result(await asyncio.to_thread(get_element_at_percent, x_percent, y_percent))


@_api_route("/api/browser/history", methods=["POST"])
async def api_history(request):
    body = await _json_body(request)
    action = body.get("action")
    if not action:
        return JSONResponse({"success": False, "error": "action is required"}, status_code=400)
    return _rest_result(await asyncio.to_thread(browser_history, action))


# ── MCP tool surface (SPEC §5 — nothing beyond this list) ────────────


@_tool()
async def navigate(url: str) -> str:
    """Navigate the persistent browser page to a URL.

    Args:
        url: Absolute URL to open (e.g. "https://example.com")

    Returns:
        JSON string with success, url, status, title
    """
    logger.info(f"navigate: {url}")
    return json.dumps(await asyncio.to_thread(navigate_browser, url))


@_tool()
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


@_tool()
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


@_tool()
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


@_tool()
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


@_tool()
async def click_at_percent(x_percent: float, y_percent: float) -> str:
    """Click at a viewport coordinate expressed as percentages.

    Args:
        x_percent: Horizontal position, 0-100
        y_percent: Vertical position, 0-100

    Returns:
        JSON string with success, resolved x/y pixels, url
    """
    return json.dumps(await asyncio.to_thread(click_browser_at_percent, x_percent, y_percent))


@_tool()
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


@_tool()
async def press_key(key: str) -> str:
    """Press a keyboard key (Enter, Tab, Escape, Backspace, ArrowDown, ...).

    Args:
        key: Playwright key name

    Returns:
        JSON string with success and url
    """
    return json.dumps(await asyncio.to_thread(press_key_in_browser, key))


@_tool()
async def scroll(delta_x: float = 0, delta_y: float = 0) -> str:
    """Scroll the page by a pixel delta.

    Args:
        delta_x: Horizontal scroll in pixels
        delta_y: Vertical scroll in pixels

    Returns:
        JSON string with success and url
    """
    return json.dumps(await asyncio.to_thread(scroll_browser, delta_x, delta_y))


@_tool()
async def history(action: str) -> str:
    """Move through the page's session history.

    Args:
        action: "back" | "forward" | "reload"

    Returns:
        JSON string with success, url, title
    """
    return json.dumps(await asyncio.to_thread(browser_history, action))


@_tool()
async def health() -> str:
    """Health check for Docker monitoring."""
    return json.dumps({
        "status": "healthy",
        "service": "agentbox",
        "version": "1.0.0",
        "browser_started": _browser_page is not None,
    })


# ── Jumpbox tools (SPEC §8/§9), gated by the §10 toggle ──────────────
#
# These are defined INSIDE the conditional on purpose. When the flag is
# off nothing here is defined, nothing is decorated, and nothing reaches
# FastMCP's registry — `list_tools()` genuinely does not contain them.
# Structuring it as a runtime check inside an always-registered tool
# would leave the tool on the surface, which SPEC §10 explicitly rules
# out.

if JUMPBOX_TOOLS_ENABLED:
    import jumpbox_tools

    logger.info(
        "Jumpbox tools ENABLED (AGENTBOX_ENABLE_JUMPBOX_TOOLS): "
        "read_file, write_file, list_directory, git, http_request"
    )

    @_tool()
    async def read_file(path: str) -> str:
        """Read a text file from the workspace.

        Args:
            path: Absolute path inside /workspace

        Returns:
            JSON string with success and content (first 1MB), or an error
        """
        return await jumpbox_tools.read_file(path)

    @_tool()
    async def write_file(path: str, content: str) -> str:
        """Write a text file inside the workspace, creating parent directories.

        Args:
            path: Absolute path inside /workspace
            content: Text to write (replaces any existing content)

        Returns:
            JSON string with success, path, bytes_written
        """
        return await jumpbox_tools.write_file(path, content)

    @_tool()
    async def list_directory(path: str) -> str:
        """List the contents of a workspace directory.

        Args:
            path: Absolute directory path inside /workspace

        Returns:
            JSON string with success and entries (name, type, size)
        """
        return await jumpbox_tools.list_directory(path)

    @_tool()
    async def git(action: str, paths: str = ".", message: str = "", count: int = 10) -> str:
        """Run one of a fixed set of git subcommands in the workspace.

        This is not a general git passthrough — only the listed actions exist.

        Args:
            action: "init" | "status" | "add" | "commit" | "diff" | "log" | "reset"
            paths: Space-separated paths for add/reset (default ".")
            message: Commit message (required for commit)
            count: Number of log entries, 1-50 (default 10)

        Returns:
            JSON string with success and output
        """
        return await jumpbox_tools.execute_git(action, paths=paths, message=message, count=count)

    @_tool()
    async def http_request(
        url: str,
        method: str = "GET",
        headers: dict | None = None,
        body: str = "",
    ) -> str:
        """Make an outbound HTTP request to an external host.

        Requests that resolve to loopback, RFC1918, link-local, or CGNAT
        addresses are refused — this cannot be used to reach internal hosts.

        Args:
            url: Absolute http:// or https:// URL
            method: "GET" or "POST"
            headers: Optional request headers
            body: Request body, for POST

        Returns:
            JSON string with success, status, headers, body (first 50k chars)
        """
        return await jumpbox_tools.execute_http_request(url, method=method, headers=headers, body=body)

else:
    logger.info(
        "Jumpbox tools DISABLED (AGENTBOX_ENABLE_JUMPBOX_TOOLS): "
        "filesystem, git and http_request tools are not registered"
    )


# ── Terminal WebSocket (SPEC §15), gated by its own toggle ───────────
#
# mcp.custom_route() builds a Starlette Route and is HTTP-only, so it
# cannot carry a WebSocket. streamable_http_app() does
# `routes.extend(self._custom_starlette_routes)` on a plain list, so a
# WebSocketRoute appended there is served alongside everything else and
# mcp.run(transport="streamable-http") stays intact.
#
# That attribute is private. tests/test_terminal.py asserts the route is
# actually reachable, so an SDK bump that renames it fails loudly rather
# than silently dropping the terminal.

if TERMINAL_ENABLED:
    from starlette.routing import WebSocketRoute

    import terminal

    mcp._custom_starlette_routes.append(
        WebSocketRoute("/terminal", endpoint=terminal.terminal_websocket)
    )

    if auth.auth_enabled():
        logger.info("Terminal ENABLED (AGENTBOX_ENABLE_TERMINAL): ws://.../terminal")
    else:
        logger.warning(
            "Terminal ENABLED but AGENTBOX_AUTH_TOKEN is not set — every "
            "connection will be refused with 4001. The terminal is fail-closed "
            "by design (SPEC §15.3); set a token to use it."
        )
else:
    logger.info(
        "Terminal DISABLED (AGENTBOX_ENABLE_TERMINAL): no /terminal route registered"
    )


if __name__ == "__main__":
    logger.info("Starting AgentBox MCP server...")
    auth.log_startup_state()
    logger.info("   Mode: Streamable HTTP")
    logger.info("   URL: http://0.0.0.0:8000/mcp")

    try:
        # mcp.run() builds the Starlette app internally, so the guard is
        # applied by wrapping the app it builds and serving that ourselves.
        # Same transport, same routes — only an extra ASGI layer in front.
        import uvicorn

        app = RebindingGuard(mcp.streamable_http_app())
        logger.info(f"   DNS-rebinding guard: hosts={sorted(ALLOWED_HOSTS)}")
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
    except KeyboardInterrupt:
        logger.info("AgentBox MCP server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
