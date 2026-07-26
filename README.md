# AgentBox

An MCP server that gives an agent a real, **persistent** Chromium browser it can
drive over Streamable HTTP — navigate, screenshot, click, read text, run JS, and
click/type/scroll by coordinate, all against the same live page across separate
tool calls. It also ships a **take-control viewer** at `/ui` so you can watch
that page live and grab the wheel yourself.

**Status: Phase 1** — local Docker only, on the operator's machine. No auth, no
cloud deployment, no Tailscale. See `docs/PRD.md` and `docs/SPEC.md`.

## How the persistence works

A dedicated daemon thread runs a forever asyncio loop that owns one Playwright
`Page` for the life of the process. Every tool dispatches into that loop via
`asyncio.run_coroutine_threadsafe`, so the page is never recreated per call and
its DOM, JS globals, cookies, and session history all survive between calls —
including across separate MCP client connections. The pattern is ported from
Hill90's `services/agentbox/app/tools.py`.

The browser is built lazily on the first tool call that needs it, and lives
until the process exits. `docker compose restart` therefore gives you a clean
browser; there is no tool that resets it.

## Quick Start

```bash
git clone <this repo> && cd AgentBox

cp .env.example .env        # optional — the defaults below are the defaults

docker compose up -d --build

curl http://localhost:8054/health
# {"status":"healthy","service":"agentbox","version":"1.0.0"}
```

**MCP endpoint**: `http://localhost:8054/mcp` — transport `streamable-http`.
**Viewer**: <http://localhost:8054/ui>

To add it to an MCP client (Claude Code, MCP Inspector, anything
Streamable-HTTP-capable):

```bash
claude mcp add --transport http agentbox http://localhost:8054/mcp
```

Configuration lives in `.env` and is read by `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `AGENTBOX_PORT` | `8054` | Host port mapped to the container's `8000` |
| `LOG_LEVEL` | `info` | Server log level |

The compose file defines one service, no external network, and one named volume
(`agentbox-screenshots` → `/workspace/screenshots`). Resource limits are 1 CPU,
1 GB memory, 200 PIDs. No Docker socket is mounted.

## MCP Tools

Exactly the eleven tools in `SPEC.md` §5 — no more:

| Tool | Arguments | Returns |
|---|---|---|
| `navigate` | `url` | `url`, `status`, `title` |
| `screenshot` | `full_page=True` | `image_base64` (PNG), `path`, `bytes`, `url` |
| `click` | `selector` | `url`, `title` |
| `get_text` | `selector="body"` | `text` (truncated at 4000 chars) |
| `evaluate` | `script` | `result` (JSON-encoded, truncated at 4000 chars) |
| `click_at_percent` | `x_percent`, `y_percent` | resolved `x`/`y` pixels, `url` |
| `type_at` | `text`, optional `x_percent`/`y_percent` | `url` |
| `press_key` | `key` (`Enter`, `Tab`, `Escape`, …) | `url` |
| `scroll` | `delta_x=0`, `delta_y=0` | `url` |
| `history` | `action`: `back` \| `forward` \| `reload` | `url`, `title` |
| `health` | — | `status`, `service`, `version`, `browser_started` |

Every tool returns a JSON string containing a `success` boolean. Failures come
back as `{"success": false, "error": "..."}` (error text capped at 300 chars)
rather than raising — a bad selector, an unreachable host, or JS that throws
leaves the browser alive and usable. `screenshot` also writes a PNG to
`/workspace/screenshots` inside the container.

`type_at` given `x_percent`/`y_percent` clicks that point to focus it first;
without them it types into whatever is already focused.

## Take-control viewer (`/ui`)

Open <http://localhost:8054/ui>. It is one static HTML page with inline JS — no
build step, no framework, no extra dependencies — modelled on Hill90's
`SessionPane.tsx` `BrowserView`.

- The screenshot polls every 2 seconds, so you watch the agent work in real time.
- Back / forward / reload buttons and a URL bar (Enter navigates; a bare host
  gets `https://` prepended).
- **Take Control** toggle. While it is on, clicking the image sends a coordinate
  click to the real page, scrolling sends a scroll (wheel events are accumulated
  and debounced 100 ms), and typing sends keystrokes — printable characters go
  to `/api/browser/type`, and Enter/Tab/Escape/Backspace/Delete/arrows/Home/End/
  PageUp/PageDown go to `/api/browser/keypress`. While it is off, the page is a
  read-only live view and no input is captured.
- Before anything has navigated, the viewer shows "Browser not active" — merely
  opening it never launches Chromium.

Crucially this is the *same* page the MCP tools drive, not a second session: an
agent can navigate over MCP and you can click the result in the viewer.

"Describe" element-picker mode from Hill90's UI is deliberately **not** built —
SPEC §6 makes it optional, and it would need an element-inspection REST route
that is not in the spec's route list.

### REST surface behind the viewer

A browser cannot speak Streamable HTTP MCP, so the viewer has its own plain HTTP
routes. Each is a thin wrapper over the very same internal function the matching
MCP tool calls — there is no second copy of the browser logic.

| Route | Body | Notes |
|---|---|---|
| `GET /ui` | — | The viewer page |
| `GET /api/screenshot` | — | `{screenshot: base64 PNG, url, bytes}`; **404** `Browser not active` until a page exists |
| `POST /api/browser/navigate` | `{url}` | |
| `POST /api/browser/click` | `{x_percent, y_percent}` | |
| `POST /api/browser/scroll` | `{delta_x, delta_y}` | Both default to 0 |
| `POST /api/browser/keypress` | `{key}` | |
| `POST /api/browser/type` | `{text}` | |
| `POST /api/browser/history` | `{action}` | `back` \| `forward` \| `reload` |

Browser-level failures return HTTP 200 with `{"success": false, "error": ...}`,
matching the MCP surface; only malformed requests get a 400. **There is no auth
on any of this** — anything that can reach the port can drive the browser, which
is acceptable only because this phase is local-Docker-only.

There is **no shell or exec tool**, and no filesystem, git, or HTTP tool. A
browser-only box has a far smaller blast radius, and adding any of those back is
a deliberate Phase 2 decision — see PRD §1.2 and SPEC §7.

## Allowlist scaffolding (`src/policy.py`)

`src/policy.py` holds two empty lists — `COMMAND_ALLOWLIST` and
`NETWORK_ALLOWLIST` — and **nothing reads them**. They are an extension point,
not a feature: if a narrow capability is ever needed (one log source, one
internal host), granting it should be a reviewed one-line data change against a
structure that already exists, rather than a redesign improvised at the time.

Two rules the module encodes: the allowlist is *data, not code*, so capabilities
arrive as small obvious diffs instead of new conditional logic at a call site;
and network enforcement must not rely on application-level checks alone, since an
in-process check can contain a structured HTTP tool but never a real shell.

Nothing enforces `NETWORK_ALLOWLIST` today. Actual Docker/firewall enforcement is
deferred to Phase 2 — there is nothing to restrict until the list has an entry
and a real deployment exists to enforce it against. The container's ordinary
outbound access is the browser tool's baseline (its job is navigating to
arbitrary URLs) and is deliberately *not* the same claim as "tool egress is
unrestricted". `tests/test_policy.py` asserts both lists are still empty, so a
future addition surfaces as an intentional diff.

There is still no `execute_command`, no shell tool, and no PTY — see PRD 1.5 for
why a full interactive shell is rejected for the agent-facing case rather than
merely postponed.

## Testing

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install pytest pytest-asyncio 'mcp>=1.2.0'
.venv-test/bin/python -m pytest tests/ -v
```

If nothing is listening on `http://localhost:8054/health`, the suite runs
`docker compose up -d --build` itself and tears it down afterwards; if the
container is already up it uses it and leaves it running. Set `AGENTBOX_URL` to
target a different host/port (tests needing local `docker top` access skip
themselves in that case).

Everything here is an integration test against a real container — there are no
mocked-browser unit tests, because the thing worth proving is that a real
Chromium page survives real tool calls.

- `tests/test_integration.py` — the happy path and the persistence proof:
  `navigate` → `screenshot` (asserts real PNG bytes) → the page is the *same*
  page, shown three ways: a JS global set in one `evaluate` call is readable by
  a later separate call, `history back` after a second `navigate` returns to the
  first URL, and the page survives a brand-new MCP connection.
- `tests/test_error_paths.py` — unreachable hosts, malformed URLs, missing
  selectors, JS that throws, unknown keys and history actions, error truncation,
  and a final check that the browser still works after all of it.
- `tests/test_resilience.py` — 120 rapid back-to-back calls with no deadlock,
  process-count checks proving repeated navigation and screenshot load leak no
  Chromium processes, a regression test that concurrent cold starts build
  exactly one browser, and `docker compose restart` recovery.
- `tests/test_ui_api.py` — the viewer and its REST routes: `/ui` is served and
  references every endpoint it needs, `navigate` → `screenshot` returns a real
  PNG (the SPEC §7 requirement, through REST instead of MCP), Take Control's
  click/type/keypress/scroll actually move the real page, REST and MCP are
  proven to drive the *same* page, and `/api/screenshot` 404s — without
  launching Chromium — until something has navigated.
- `tests/test_policy.py` — the allowlist trip-wire: both lists still empty, and
  the server still does not reference them. Needs no container.

## Architecture

```
python:3.12-slim-bookworm
├── Chromium system libs (apt — see Dockerfile)
├── Playwright + Chromium browser (/data/browsers)
├── Python deps (fastmcp, pydantic, uvicorn, playwright)
└── src/
    ├── mcp_server.py
    │   ├── persistent browser loop (daemon thread, owns the Page)
    │   ├── FastMCP streamable-http on :8000 → :8054
    │   └── plain REST routes for the viewer (same internal functions)
    ├── policy.py  (empty allowlist scaffolding — unused by design)
    └── ui.html    (served at /ui, inline JS, no build step)
```

Debian is required: Playwright/Chromium does not run on musl libc, so an Alpine
base cannot work here. Bookworm is pinned deliberately — bare `python:3.12-slim`
now resolves to trixie, where `libasound2` is renamed `libasound2t64` and the
package list in `SPEC.md` §4 fails to install.

### Known limitations (Phase 1, accepted)

- **One page, one client.** All callers share a single page, so two clients
  driving it at once will interfere. That is the point for the viewer — you and
  the agent share a page deliberately — but firing several `navigate` calls
  concurrently makes Chromium abort the superseded ones with `ERR_ABORTED`;
  that is normal single-page behaviour, and each caller gets a clean error.
- **The viewer is a screenshot poll, not a video stream.** Updates land about
  every 2 seconds (immediately after your own clicks and keystrokes), so fast
  animations and hover states are not faithfully represented.
- **No crash recovery.** If Chromium itself dies, tools keep failing until the
  container is restarted; the server does not rebuild the page.
- **No auth.** Anything that can reach the port can drive the browser. That is
  acceptable only because this phase is local-Docker-only.

## Troubleshooting

```bash
docker compose logs -f                 # logs
curl http://localhost:8054/health      # health
docker compose exec agentbox bash      # shell inside the container
docker top agentbox -eo pid,comm       # Chromium processes (image has no ps)
docker compose restart                 # restart — also gives a fresh browser
```

## Files

```
agentbox/
├── Dockerfile              # Debian slim + Playwright/Chromium
├── docker-compose.yml      # single service, local only
├── pyproject.toml          # Python dependencies + pytest config
├── .env.example            # AGENTBOX_PORT, LOG_LEVEL
├── docs/                   # PRD.md, SPEC.md
├── src/mcp_server.py       # FastMCP server + persistent browser loop
└── tests/
    ├── conftest.py            # container fixture + MCP client helpers
    ├── test_integration.py    # happy path + persistence proof
    ├── test_error_paths.py    # failure modes
    └── test_resilience.py     # load, leaks, restart
```
