# AgentBox

An MCP server that gives an agent a real, **persistent** Chromium browser it can
drive over Streamable HTTP — navigate, screenshot, click, read text, run JS, and
click/type/scroll by coordinate, all against the same live page across separate
tool calls. It also ships a **take-control viewer** at `/ui` so you can watch
that page live and grab the wheel yourself, and — behind a feature toggle —
workspace-scoped filesystem, git, and SSRF-protected HTTP tools.

**Status: Phase 1** — local Docker only, on the operator's machine. No auth, no
cloud deployment, no Tailscale. See `docs/PRD.md` and `docs/SPEC.md`.

AKM/knowledge tools are permanently out of scope. A real interactive terminal is
a separate, deliberate build that does not exist yet.

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
| `AGENTBOX_ENABLE_JUMPBOX_TOOLS` | `true` | Register the filesystem/git/http tools (see below) |
| `AGENTBOX_AUTH_TOKEN` | *(empty)* | Shared secret. Empty = no auth (see below) |
| `AGENTBOX_ENABLE_TERMINAL` | `false` | Register the PTY WebSocket (see below) |

The compose file defines one service, no external network, and two named volumes:
`agentbox-workspace` → `/workspace` and `agentbox-screenshots` →
`/workspace/screenshots`. Files written by `write_file` and commits made by the
`git` tool therefore survive `docker compose down`; remove the volume
(`docker volume rm agentbox_agentbox-workspace`) to start clean. Resource limits
are 1 CPU, 1 GB memory, 200 PIDs. No Docker socket is mounted.

## MCP Tools

### Core browser tools (always registered)

The eleven tools from `SPEC.md` §5. These ship unconditionally:

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

### Jumpbox tools (behind `AGENTBOX_ENABLE_JUMPBOX_TOOLS`)

These five are registered **only when the toggle is on** — see the next section.

| Tool | Arguments | Notes |
|---|---|---|
| `read_file` | `path` | First 1 MB; must be inside `/workspace` |
| `write_file` | `path`, `content` | Creates parent directories; `/workspace` only |
| `list_directory` | `path` | `name`, `type`, `size` per entry |
| `git` | `action`, `paths`, `message`, `count` | Fixed set: `init`, `status`, `add`, `commit`, `diff`, `log`, `reset` |
| `http_request` | `url`, `method`, `headers`, `body` | GET/POST only; SSRF-blocked |

Containment, which is the interesting part:

- **`PathPolicy` scopes the filesystem tools to `/workspace`.** Paths are
  `realpath`-resolved before checking, so `..` traversal and symlinks are judged
  on their target, and `/workspaceless` is not treated as a child of
  `/workspace`. Default-deny: a path must match an allowed root.
- **`git` is a fixed subcommand set, not a passthrough.** There is no
  `git <arbitrary args>`; unknown actions are refused. Adding a subcommand is a
  reviewed code change.
- **`http_request` resolves the hostname and checks the resolved IP** against
  loopback, RFC1918, link-local (including cloud metadata at 169.254.169.254),
  and the 100.64.0.0/10 CGNAT range Tailscale uses — so a DNS name pointing at
  an internal address does not get through. GET and POST only.
- **Redirects are followed one hop at a time and every hop is re-checked.** A
  public URL that 302s to `http://169.254.169.254/` is refused at the redirect,
  with the same `{"success": false, "error": ...}` shape as a direct block.
  Chains are capped at 3 hops.

## Feature toggle

`AGENTBOX_ENABLE_JUMPBOX_TOOLS` is read **once at startup** and defaults to
`true` for local dev. `docker-compose.yml` and `.env.example` set it explicitly,
so the "on for local dev" fact lives in deployment config rather than in code.

When it is off, the five tools above are **not registered with FastMCP at all** —
they are absent from `list_tools()` and calling one is an unknown-tool error, not
a "disabled" response. They are defined inside the conditional, so nothing exists
to reach. The core browser tools and `/ui` are unaffected.

```bash
AGENTBOX_ENABLE_JUMPBOX_TOOLS=false docker compose up -d
docker compose logs | grep Jumpbox     # "Jumpbox tools DISABLED"
```

`tests/test_feature_toggle.py` proves both directions by running a second
container with the flag flipped and comparing the real `list_tools()` surface.
Any deployment profile other than local dev must default this to off, as its own
reviewed decision (PRD 1.9).

## Auth

Off by default: with `AGENTBOX_AUTH_TOKEN` empty or unset, nothing is gated and
the server behaves exactly as it did before this existed. The guard is not even
wired in — the decorators that would attach it are pass-throughs.

Set it to any shared secret and every **MCP tool** and every **`/api/*` route**
requires `Authorization: Bearer <token>`. Missing, malformed (no `Bearer `
prefix), or wrong tokens get a structured 401:

```json
{"success": false, "status": 401, "error": "Unauthorized: missing or invalid Bearer token"}
```

REST routes send that with an actual HTTP 401; MCP tools return it as their JSON
result, since a tool result has no status line of its own.

Deliberately left open:

- `GET /health` — the Docker healthcheck target, not a capability (Hill90 leaves
  it open too). Note the MCP `health` *tool* is gated; only the route is not.
- `GET /ui` — the page has to load before it can ask for a token.
- `GET /api/auth-required` — how the page learns whether to prompt. It answers
  only "yes/no", which a 401 would reveal anyway.

The viewer prompts once when the server reports auth is on, keeps the token in
`sessionStorage`, and attaches it to every `/api/*` call afterwards. A 401 clears
it and re-prompts, so a mistyped token is recoverable without a reload.

```bash
AGENTBOX_AUTH_TOKEN=some-shared-secret docker compose up -d
curl http://localhost:8054/health                                    # 200, no token
curl -X POST http://localhost:8054/api/browser/navigate \
  -H 'Authorization: Bearer some-shared-secret' \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/"}'
```

This is a **shared secret, not OAuth** — ported from Hill90's `WORK_TOKEN`
pattern. OAuth/DCR is Phase 2's problem. The token is built to be reused rather
than replaced: it is the credential an OAuth wrapper would sit in front of, it is
the shape Anthropic's `static_headers` connector auth expects, and the terminal's
WebSocket will check the same secret as a `?token=` param when it exists.

The comparison is constant-time (`secrets.compare_digest`).

## Terminal

A real PTY over a WebSocket at `/terminal`, with an xterm.js panel in `/ui`.
Ported from hill90-app's `ws_terminal.py`.

**Off by default, and gated twice.** This is the only toggle here that defaults
to `false` — everything else is a structured tool; this one is a shell.

1. `AGENTBOX_ENABLE_TERMINAL=true`, or the WebSocket route is never registered
   and there is nothing to connect to.
2. `AGENTBOX_AUTH_TOKEN` must be set, and the socket must supply it as
   `?token=<token>`. This is **fail closed**: unlike the HTTP surface, where no
   token means no auth, a terminal with no token configured refuses *every*
   connection. Hill90 never exposes this socket unauthenticated even to itself.

```bash
AGENTBOX_ENABLE_TERMINAL=true AGENTBOX_AUTH_TOKEN=some-secret docker compose up -d
# then open /ui and click Terminal
```

Wire format: binary frames are raw terminal I/O, text frames are JSON control
messages (`{"type":"resize","cols":N,"rows":N}`). Unknown control frames are
ignored rather than rejected. The shell is `tmux new-session -A` where
available, so a dropped socket reattaches instead of losing the session.

`/ui` presents Browser and Terminal as **tabs**, each owning the full pane —
the terminal does not squash the browser view. The terminal keeps its WebSocket
open while you are on the Browser tab, so switching back does not drop the shell
session. The tab is only shown when the server reports the toggle on.

The terminal is Observing by default; **Take Control** enables stdin, matching
the browser view's model.

**Theme:** tmux runs `fabioluciano/tmux-tokyo-night` pinned to `fcfde9a` — the
same commit and the same mods (night variation, transparent status bar, status
at top, empty separators) as hill90-app's agentbox, so it looks like the
terminal there. The powerline glyphs need a Nerd Font; rather than vendoring
2.1 MB, `src/vendor/nerd-symbols.woff2` is a **944-byte subset** of just the six
codepoints the theme actually draws. An auth refusal arrives as an HTTP 403 handshake rejection
rather than close code 4001 — the server rejects *before* accepting, so no
socket is ever opened — and the client treats that as "do not retry".

The shell's environment is built explicitly rather than inherited, so a terminal
session cannot read `AGENTBOX_AUTH_TOKEN` out of the server process.

**The shell is not root.** The container runs as an unprivileged `agentbox`
user (uid 1000) which owns `/workspace`, the screenshots volume and the
Playwright browser cache. `docker-entrypoint.sh` fixes volume ownership as root
and then drops privileges with `setpriv --inh-caps=-all` before the server
starts, so a volume created by an older root-running build keeps working. This
matches Hill90's `agentuser`.

xterm.js is **vendored** into `src/vendor/`, not loaded from a CDN, so `/ui`
keeps working with no outbound network and no build step.

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
- **Describe** toggle, next to Take Control and only offered while it is on.
  With Describe on, clicking the image inspects the element under the pointer
  instead of clicking it: a popover shows the tag, id, classes, text and a
  suggested CSS selector, with the element outlined on the screenshot. The page
  itself is never touched, and keystrokes are not forwarded while it is on.
- Before anything has navigated, the viewer shows "Browser not active" — merely
  opening it never launches Chromium.

Crucially this is the *same* page the MCP tools drive, not a second session: an
agent can navigate over MCP and you can click the result in the viewer.

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
| `POST /api/browser/element` | `{x_percent, y_percent}` | Describe mode: element info, no click |

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
.venv-test/bin/pip install pytest pytest-asyncio 'mcp>=1.2.0' httpx websockets
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
- `tests/test_policy.py` — the allowlist trip-wire (both lists still empty and
  unreferenced in code) plus `PathPolicy` unit tests: traversal, symlink
  resolution, prefix-is-not-a-child, denied-beats-allowed, read-only. Needs no
  container.
- `tests/test_jumpbox_tools.py` — the gated tools' real behaviour: filesystem
  round-trips, every escape attempt out of `/workspace` refused, git's fixed
  subcommand set, and the SSRF guard against loopback/RFC1918/link-local/CGNAT.
- `tests/test_ssrf_redirects.py` — the redirect hops specifically, against a
  throwaway loopback redirect server. Deterministic and offline: no container
  and no network needed.
- `tests/test_terminal.py` — three containers (toggle off, on without a token,
  on with one): route absence, fail-closed refusal, wrong/missing token, an echo
  round-trip through the real PTY, `pwd` landing in `/workspace`, resize moving
  the child's terminal size, tolerance of unknown control frames, the auth token
  not leaking into the shell, and no shell processes leaking across sessions.
  Needs `pip install websockets`.
- `tests/test_auth.py` — auth off by default, and with a second container
  running `AGENTBOX_AUTH_TOKEN`: every `/api/*` route and MCP tool refused
  without a token, refused with a wrong one, refused with a raw token lacking
  the `Bearer ` prefix, accepted with the right one, and `/health` open
  throughout.
- `tests/test_feature_toggle.py` — the §10 trip-wires. Runs a second container
  with `AGENTBOX_ENABLE_JUMPBOX_TOOLS=false` and asserts the five tools are
  genuinely gone from `list_tools()`, that calling one is an unknown-tool error,
  and that the browser, `/ui` and `/health` still work with everything off.

## Architecture

```
python:3.12-slim-bookworm  (runs as `agentbox`, uid 1000 — not root)
├── Chromium system libs (apt — see Dockerfile)
├── Playwright + Chromium browser (/data/browsers)
├── tmux + zsh, Tokyo Night theme pinned to fcfde9a
├── Python deps (fastmcp, pydantic, uvicorn, playwright, httpx)
└── src/
    ├── auth.py           (Bearer token check — SPEC §12)
    ├── terminal.py       (PTY WebSocket relay — SPEC §15)
    ├── vendor/           (xterm.js, vendored — see its README)
    ├── mcp_server.py
    │   ├── persistent browser loop (daemon thread, owns the Page)
    │   ├── FastMCP streamable-http on :8000 → :8054
    │   └── plain REST routes for the viewer (same internal functions)
    ├── policy.py         (allowlist scaffolding + PathPolicy)
    ├── jumpbox_tools.py  (filesystem, git, http_request — toggle-gated)
    └── ui.html           (served at /ui, inline JS, no build step)
```

Debian is required: Playwright/Chromium does not run on musl libc, so an Alpine
base cannot work here. Bookworm is pinned deliberately — bare `python:3.12-slim`
now resolves to trixie, where `libasound2` is renamed `libasound2t64` and the
package list in `SPEC.md` §4 fails to install.

### Known limitations (Phase 1, accepted)

- **No multi-user story.** One shared browser page, one shared workspace, one
  shared token. This is a single-operator box.

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
├── Dockerfile              # Debian slim + Playwright/Chromium + tmux/zsh
├── docker-entrypoint.sh    # fixes volume ownership, then drops root
├── theme/                  # tmux Tokyo Night + zshrc (SPEC §15.6)
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
