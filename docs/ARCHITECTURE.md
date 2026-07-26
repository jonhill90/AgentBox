# Architecture

How the pieces fit. For *why* the security choices are what they are, see
`SECURITY.md`; for what is in scope, `SPEC.md`.

## One process, three surfaces

A single Python process serves everything, on one port:

```
                        ┌──────────────────────────────────┐
  MCP client ──────────►│ /mcp        Streamable HTTP (MCP) │
  (Claude Code)         │                                   │
                        │ /api/*      REST, for the viewer  │
  Browser ─────────────►│ /ui         the viewer page       │
                        │ /terminal   WebSocket PTY         │
                        │ /health     unauthenticated       │
                        └───────────────┬──────────────────┘
                                        │  all of these call the SAME
                                        │  internal functions
                                        ▼
                        ┌──────────────────────────────────┐
                        │ persistent browser loop (thread) │
                        │   owns ONE Playwright Page       │
                        └──────────────────────────────────┘
```

The REST surface exists because a browser cannot speak Streamable HTTP MCP.
It is a thin wrapper over the same functions the MCP tools call — there is no
second implementation of the browser logic, and both surfaces drive the same
page. Tests assert this cross-surface identity directly.

## The persistent browser loop

This is the core idea and the reason the project exists.

A Playwright `Page` is bound to the asyncio event loop that created it.
FastMCP serves each request on its own loop, so a page created inside one
request handler is not safely usable from the next. The fix, ported from
hill90-app's `services/agentbox/app/tools.py`:

- A dedicated daemon thread runs a `run_forever` asyncio loop.
- That loop owns the Playwright instance, browser, context, and the single
  `Page`, for the life of the process.
- Every caller dispatches in with `asyncio.run_coroutine_threadsafe`.
- MCP tool handlers wrap that blocking dispatch in `asyncio.to_thread`, so
  the server's own loop is never stalled.

The page is created lazily on first use and never recreated. DOM state, JS
globals, cookies and session history therefore survive across separate tool
calls *and* across separate client connections. `tests/test_integration.py`
proves this three ways, including that a JS global set in one `evaluate` call
is readable by a later, separate one.

An `asyncio.Lock` guards first-time creation. Without it, concurrent first
calls each launched their own browser and clobbered each other's globals —
that was a real bug, and `test_concurrent_cold_start_launches_exactly_one_browser`
is its regression test.

## Modules

| File | Responsibility |
|---|---|
| `src/mcp_server.py` | Tool + route registration, the browser loop, feature toggles, the DNS-rebinding middleware |
| `src/auth.py` | The one bearer-token comparison, token-file loading, rotation overlap |
| `src/terminal.py` | PTY WebSocket relay and its auth handshake |
| `src/jumpbox_tools.py` | Filesystem, git, `http_request` — everything behind the jumpbox toggle |
| `src/policy.py` | `PathPolicy` (live) and the empty command/network allowlists (scaffolding) |
| `src/ui.html` | The viewer: one static page, inline JS, no build step |
| `src/vendor/` | xterm.js and its fit addon, vendored so `/ui` works offline |

## Registration and toggles

Tools and routes are registered through two small wrappers rather than the
FastMCP decorators directly:

- `_tool(...)` — `@mcp.tool()` plus the auth guard when auth is enabled
- `_api_route(...)` — `@mcp.custom_route()` plus the same guard

When `AGENTBOX_AUTH_TOKEN` is unset these are pass-throughs, so behaviour is
identical to a build without auth. Applying the guard at registration means
there is one place to get it right instead of one per tool.

Feature-gated tools are **defined inside the conditional**, so when a toggle
is off nothing is defined, decorated, or registered:

```python
if JUMPBOX_TOOLS_ENABLED:
    @_tool()
    async def read_file(path: str) -> str: ...
```

The terminal is registered differently because `mcp.custom_route()` is
HTTP-only. `streamable_http_app()` extends a plain `_custom_starlette_routes`
list, so a `WebSocketRoute` appended there is served alongside everything
else. That attribute is private, so a test asserts the route is genuinely
reachable rather than trusting it survives an SDK bump.

## Request path

Every HTTP and WebSocket request passes through `RebindingGuard`, an ASGI
middleware wrapping the whole app, before routing. It validates `Origin` and
`Host` against an allowlist. This is why the server is started by handing
`mcp.streamable_http_app()` to uvicorn rather than calling `mcp.run()` —
FastMCP rebuilds the Starlette app on each call, so there is no cached
instance to wrap.

## The viewer

`/ui` is one static HTML file with inline CSS and JS. No framework, no build
step. It models hill90-app's `SessionPane.tsx` without React:

- **Browser tab** — polls `/api/screenshot` every 2s and repaints; chrome bar
  with back/forward/reload and a URL field; a **Take Control** toggle that
  forwards clicks, scroll (wheel events debounced 100 ms) and keystrokes; a
  **Describe** toggle that inspects the element under the pointer instead of
  clicking it.
- **Terminal tab** — xterm.js over the `/terminal` WebSocket, Observing by
  default with its own Take Control.

The two are **tabs**, each owning the full pane. The terminal keeps its
WebSocket open while the Browser tab is showing, so switching does not drop
the shell session.

## Container

Debian slim (`python:3.12-slim-bookworm`, pinned — trixie renames
`libasound2`). Playwright/Chromium cannot run on musl, so Alpine is not an
option. Also installed: tmux, zsh, git, FiraCode Nerd Font, Powerlevel10k and
oh-my-zsh, so the terminal looks like the operator's own.

`docker-entrypoint.sh` runs as root only long enough to fix ownership of the
mounted volumes, then drops to the `agentbox` user (uid 1000) with
`setpriv --inh-caps=-all`. An entrypoint rather than a bare `USER` directive
because a named volume created by an earlier root-running build stays
root-owned, and a plain switch would leave the server unable to write its own
workspace.
