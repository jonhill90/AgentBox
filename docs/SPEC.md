# docs/SPEC.md — AgentBox Phase 1 Engineering Spec

Build from this spec and `docs/PRD.md` — not from conversation history or
assumptions about what Hill90 does. If the spec is missing something
you need, stop and flag it rather than guessing.

## 1. System Overview

Single-service Python app (FastMCP + Playwright). Docker-only. No
orchestration beyond the one container, no other services, no
network dependency besides pulling the base image and whatever page
it's told to navigate to. Runs on the operator's machine via
`docker-compose up`.

## 2. Source of the browser tool (PRD 1.1)

Port — do not rewrite from scratch — the persistent-loop browser tool
from Hill90's `services/agentbox/app/tools.py`
(`/Users/jon/source/repos/Personal/hill90-app/services/agentbox/app/tools.py`,
read-only reference; do not modify that repo). Specifically bring
over:

- `_browser_loop` / `_browser_loop_thread` — the persistent asyncio
  event loop running in a background thread, which owns the single
  Playwright `Page` for the process lifetime. This is the piece that
  makes the browser survive across separate tool calls; do not
  simplify it into a per-call browser launch.
- `_run_on_browser_loop_sync` — thread-safe dispatch into that loop.
- `_ensure_browser_page_on_loop`, `_capture_screenshot`.
- `navigate_browser`, `click_browser_at_percent`,
  `get_element_at_percent`, `type_in_browser`, `press_key_in_browser`,
  `scroll_browser`, `browser_history`.
- The `_execute_browser` dispatcher shape (navigate / screenshot /
  click-by-selector / get_text / evaluate).

Hill90 wires these into its own `AgentRuntime`/tool-registry
abstraction and a Starlette custom-dispatch model. This repo does not
have that abstraction — adapt the call sites to FastMCP's
`@mcp.tool()` decorator model, which is this repo's existing pattern
in `src/mcp_server.py`. The browser-loop internals port as-is; only
the tool-registration glue changes.

## 3. Server & transport

- FastMCP, `mcp.run(transport="streamable-http")` — matches the
  existing `src/mcp_server.py` skeleton. Keep its shape: the
  `/health` custom route, the logging setup, `PYTHONUNBUFFERED=1`.
- Replace the existing tool set (`execute_command`, `manage_process`)
  entirely with the browser tools from §5. Do not keep the old tools
  around "just in case."

## 4. Docker image

- **Base image change required:** Playwright/Chromium does not run on
  musl libc, so the current `python:3.11-alpine` base cannot work
  here. Switch to `python:3.12-slim`, matching what Hill90's
  `Dockerfile` already proves out.
- Install Playwright + Chromium using the same apt package list
  Hill90's `Dockerfile`/`Dockerfile.browser` already validates
  (`libnss3`, `libnspr4`, `libatk1.0-0`, `libatk-bridge2.0-0`,
  `libcups2`, `libdrm2`, `libxkbcommon0`, `libxcomposite1`,
  `libxdamage1`, `libxrandr2`, `libgbm1`, `libpango-1.0-0`,
  `libcairo2`, `libasound2`, `libxshmfence1`, `libxfixes3`,
  `fonts-liberation`) — copy this list verbatim rather than
  rediscovering it by trial and error.
- Drop the Docker-CLI / `/var/run/docker.sock` mount from the current
  `docker-compose.yml`. This box does not manage other containers.
- Keep the existing resource limits (cpus/memory/pids) in
  `docker-compose.yml`.
- Update `pyproject.toml` dependencies: add `playwright`, remove
  anything that only existed for `execute_command`/`manage_process`.

## 5. Tool surface (core list)

`navigate`, `screenshot`, `click` (by selector), `get_text`,
`evaluate` (JS), `click_at_percent`, `type_at`, `press_key`, `scroll`,
`history` (back/forward/reload), `health`. These ship unconditionally,
same as before.

§8/§9 add filesystem, git, and `http_request` tools **behind the
feature-toggle framework in §10** — additive, not a change to this
core list. A future terminal is its own separate build, also
toggle-gated. AKM/knowledge tools remain permanently out of scope —
never add them, don't flag them as a future maybe.

## 6. Take-control viewer UI (PRD 1.4)

The MCP tool surface (§5) is machine-facing only — a browser can't
speak Streamable HTTP MCP directly. The UI needs its own plain HTTP
surface that calls the *same internal Python functions* the MCP tools
already wrap (`navigate_browser`, `click_browser_at_percent`,
`type_in_browser`, `press_key_in_browser`, `scroll_browser`,
`browser_history`, plus a screenshot getter) — do not have the UI
speak MCP itself, and do not duplicate the browser-loop logic. This
mirrors how Hill90's own UI works: its Next.js API routes call into
the agent's tool implementation directly, not through MCP.

- Add plain Starlette routes to `src/mcp_server.py` via
  `@mcp.custom_route(...)` (same pattern as the existing `/health`
  route): `GET /ui` (serves one static HTML page, inline JS, no
  build step / no Next.js needed for this), `GET /api/screenshot`,
  `POST /api/browser/navigate`, `POST /api/browser/click`
  (`x_percent`/`y_percent`), `POST /api/browser/scroll`,
  `POST /api/browser/keypress`, `POST /api/browser/type`,
  `POST /api/browser/history`. Each of these is a thin wrapper calling
  the existing internal function — no new browser logic.
- `GET /api/screenshot` returns 404 with a clear message if
  `_browser_page` is `None` yet (mirrors Hill90's "Browser not active"
  state), otherwise the current PNG (base64 or raw bytes — client's
  choice) plus the current URL.
- Frontend behavior to match, from Hill90's
  `services/ui/src/app/chat/SessionPane.tsx` `BrowserView` (read-only
  reference): poll `/api/screenshot` every ~2s and repaint the image;
  chrome bar with back/forward/reload buttons and a URL input that
  navigates; a **Take Control** toggle — when on, clicking the
  screenshot image posts a coordinate click, scrolling the image posts
  a scroll (debounce rapid wheel events), and keydown while the image
  area is focused posts a keypress or a typed character. When Take
  Control is off, the image is just a live view, no input is captured.
- "Describe" element-picker mode is optional for this pass — build
  screenshot + chrome + Take Control first; add Describe only if it
  doesn't cost much more.
- No auth on any of this (matches PRD 1.3/1.4) — it's a local dev tool.

## 7. Jumpbox allowlist scaffolding (PRD 1.5) — mechanism only

This grants no new capability. It builds the extension points so a
future narrow capability (a specific log source, a specific reachable
host) is a small config change later, not a redesign.

- New module `src/policy.py`:
  - `COMMAND_ALLOWLIST: list[dict]` — empty list to start. Each future
    entry is `{"binary": "<resolved-or-resolvable-path>", "args": [...]}`
    shape, checked the way Hill90's `CommandPolicy.check()` does
    (resolve to a real absolute path via `shutil.which`, reject
    anything not on the list, `shell=False`, scrubbed env if anything
    is ever added). No caller in this codebase invokes it yet — there
    is no `execute_command` tool or route. It exists so that adding one
    later means adding a policy-checked call site, not building the
    checking logic from scratch under time pressure.
  - `NETWORK_ALLOWLIST: list[str]` — empty list to start (or, if the
    container's default network access already permits open internet
    egress for browser navigation, document that explicitly here as
    the current baseline and treat this list as *additional* specific
    hosts beyond that baseline — do not conflate "browser can navigate
    anywhere on the open internet" with "shell/tool egress is
    unrestricted").
  - A short docstring/comment in the module explaining the two rules
    from PRD 1.5: allowlist is data, not code; network enforcement
    must not rely on application-level checks alone.
- Do not wire a Docker-level firewall/iptables rule for this yet —
  there's nothing to restrict until `NETWORK_ALLOWLIST` has an entry
  and there's an actual deployment target (Phase 2) with a real network
  boundary to enforce it against. Document in `src/policy.py` that this
  is deferred, and why (§14 item 3 already tracks the real version of
  this for Phase 2).
- One test: `COMMAND_ALLOWLIST == []` and `NETWORK_ALLOWLIST == []` (or
  whatever documented baseline) as of this commit — a trivial assertion,
  but it's the trip-wire that makes a future accidental addition show
  up as an intentional diff instead of silent scope creep.

## 8. Filesystem and git tools (PRD 1.7)

Port, don't rewrite, from Hill90 (read-only reference —
`/Users/jon/source/repos/Personal/hill90-app/services/agentbox/app/`):

- `filesystem.py` → `read_file`, `write_file`, `list_directory`. Port
  `PathPolicy` from Hill90's `policy.py` alongside it (realpath-resolved
  allow/deny lists, explicit read-only mode) — this repo's own
  `src/policy.py` currently only has `COMMAND_ALLOWLIST`/
  `NETWORK_ALLOWLIST` from §7; add `PathPolicy` as its own class in the
  same module, it's a different mechanism (path scoping, not an
  allowlist of specific commands).
- Configure `PathPolicy` scoped to a single workspace directory (e.g.
  `/workspace`, matching the volume already in `docker-compose.yml`) —
  not `/`, not the container's own source tree.
- `tools.py`'s `_execute_git` → a `git` tool exposing exactly the
  existing fixed subcommand set (`init`, `status`, `add`, `commit`,
  `diff`, `log`, `reset`), scoped to that same workspace directory.
  This is not a general git passthrough — do not add `git <arbitrary
  args>`; if a subcommand isn't in that list, it isn't supported.
- All three filesystem functions and the git tool are registered as
  MCP tools **only when the feature-toggle flag from §10 is on** —
  wire the toggle check in from the start, don't add these unguarded
  and gate them in a later pass.

## 9. `http_request` tool (PRD 1.8)

Port Hill90's `_execute_http_request` and `_is_blocked_host` verbatim
in logic: resolve the target hostname, check the resolved IP against
the blocklist (loopback `127.0.0.0/8`, RFC1918 `10.0.0.0/8` /
`172.16.0.0/12` / `192.168.0.0/16`, link-local `169.254.0.0/16`, and
the Tailscale CGNAT range `100.64.0.0/10`), reject if blocked,
otherwise perform the request. Also gated by the §10 toggle.

`NETWORK_ALLOWLIST` (§7) is where a future specific exception (e.g.
`dev.debatewho.com`, once Phase 2 exists and this tool needs to reach
it) gets added — as an explicit carve-out checked *before* the
blocklist rejects it, not by removing anything from the blocklist
itself. No such exception exists yet; don't add one speculatively.

## 10. Feature-toggle framework (PRD 1.9)

Governs §8 and §9 now, and the terminal in a later pass.

- Add `AGENTBOX_ENABLE_JUMPBOX_TOOLS` (env var, default `"true"` for
  local `docker-compose` dev — set it explicitly in
  `docker-compose.yml`/`.env.example` rather than relying on the
  Python-side default, so the "on for local dev" fact is visible in
  the deployment config, not buried in code).
  read once at server startup in `src/mcp_server.py`.
- When the flag is off: the filesystem tools, the git tool, and the
  `http_request` tool are **not registered** with FastMCP at all —
  structure the registration so this is a conditional `@mcp.tool()`
  application (or an equivalent "only define/register if enabled"
  pattern), not a runtime check inside an always-registered tool that
  returns an error when disabled. The tool must not exist on the MCP
  surface when off, not exist-but-refuse.
- Trip-wire tests: with the flag off (test sets the env var before the
  container starts, or exercises whatever the equivalent local check
  is), assert `list_tools()` does not include `read_file`,
  `write_file`, `list_directory`, `git`, or `http_request`. With the
  flag on (the default, so this is what the existing suite already
  exercises), assert they do.
- Do not flip the default off as part of this work — §10's job is
  building the toggle and proving it works both ways with tests, not
  changing today's local-dev behavior. Actually running with it off to
  "prove DebateWho-off is real" (PRD 1.9's last requirement) happens
  later, deliberately, right before Phase 2 planning — not bundled into
  this commit.

## 11. Describe element-picker mode (PRD 1.4, deferred from §6)

Promote this out of "optional, skip if not quick" now that it's
explicitly wanted:

- Expose Hill90's `get_element_at_percent` (already ported and present
  in `src/mcp_server.py`, currently unused by any route) as
  `POST /api/browser/element` — thin wrapper, same pattern as the
  other `/api/browser/*` routes from §6.
- Frontend: a **Describe** toggle next to Take Control (only visible/
  usable when Take Control is on, matching Hill90's
  `SessionPane.tsx` behavior). When Describe is on, clicking the image
  calls `/api/browser/element` instead of posting a real click, and
  shows a small popover near the click point with the element's tag,
  id, classes, text, and selector — read-only info, no chat/annotation
  send step is needed here (Hill90's version sends the description to
  its own chat thread, which doesn't exist in this standalone repo;
  just display the info in the popover).
- This is browser-tool-surfaced information only (`get_element_at_percent`
  doesn't touch the filesystem/git/http_request/network — it reads DOM
  info from the page already being driven). It is **not** gated by the
  §10 toggle; it's part of the core browser/UI feature set from §6,
  same trust tier as `screenshot` or `click`.

## 12. Local auth layer (PRD 1.10)

Port Hill90's `WORK_TOKEN` pattern (read-only reference:
`services/agentbox/app/runtime.py`'s `_check_auth`, `server.py`'s
`os.environ.get("WORK_TOKEN")` and its use in both the `/work` endpoint
and `ws_terminal_handler`).

- `AGENTBOX_AUTH_TOKEN` env var, optional, empty/unset by default —
  auth is off, matching every prior test in this suite; do not change
  their behavior.
- A single check function (in `src/policy.py`, alongside the other
  policy primitives, or a new small `src/auth.py` — your call, keep it
  in one obvious place) mirroring `_check_auth`: reads the
  `authorization` header, requires the `Bearer ` prefix, compares to
  `AGENTBOX_AUTH_TOKEN`. Returns `False` (deny) if the token isn't
  configured at all — but only *enforce* that denial where the caller
  actually wired the check in; if `AGENTBOX_AUTH_TOKEN` is unset, don't
  wire the check in at all, so behavior is identical to today.
- Wire the check into: every `@mcp.tool()` (FastMCP middleware/dependency
  if it supports one cleanly, otherwise a shared helper called at the
  top of each tool) and every `/api/*` custom route from §6. `/health`
  is exempt, same as Hill90.
- 401 with a structured `{"success": false, "error": "..."}` body (MCP
  tool calls) or an equivalent JSON 401 (REST routes) on a missing or
  wrong token — never a bare exception.
- `/ui`: if a small unauthenticated `GET /api/auth-required` (or similar)
  reports auth is on, prompt once for the token, store it in
  `sessionStorage`, attach `Authorization: Bearer <token>` to every
  subsequent `/api/*` fetch call. Don't prompt per-request.
- Tests: with `AGENTBOX_AUTH_TOKEN` unset (today's default), the full
  existing suite must pass unchanged. With it set (a second container
  run, same pattern as §10's toggle tests), assert unauthenticated
  calls to a gated MCP tool and a gated REST route both fail with 401,
  and that the correct Bearer token succeeds. Also assert `/health`
  still works with no token, auth on or off.
- This token is designed to be reusable, not a throwaway: it's the
  same shape the eventual terminal's WebSocket gate needs (§14 item 6),
  and it's also exactly what Anthropic's `static_headers` connector
  auth (currently beta) expects a self-hosted MCP server to check —
  building it now means Phase 2 has a real credential to plug in
  rather than a design question to answer from scratch.

## 13. Non-Functional Requirements

- Must run entirely locally via `docker-compose up` — no cloud
  dependency, no Tailscale, no auth layer beyond §12's opt-in token.
  It is fine (expected) that
  this container is reachable only on the operator's local Docker
  network in this phase.
- Tests: at minimum, one integration test that starts the container,
  connects an MCP client, calls `navigate` then `screenshot`, and
  asserts a non-empty PNG comes back. This is the test that actually
  proves the persistent-loop pattern works — a test that only checks
  the server imports or `/health` responds is not sufficient evidence
  of Phase 1 being done.
- For the UI (§6): at least one test that drives `/api/browser/navigate`
  then reads `/api/screenshot` and gets a real PNG back — the same
  persistence proof as the MCP-side tests, just through the REST
  surface instead.
- Update `README.md` to describe the new tool set and drop the
  `execute_command`/`manage_process` documentation.

## 14. Open Items (Phase 2 — do not design or build yet)

1. OAuth wrapper approach (Cloudflare Worker `workers-oauth-provider`
   template vs. hand-rolled DCR/CIMD) — §12's bearer token becomes the
   credential this fronts, per the connector-auth research: it's already
   the shape Anthropic's `static_headers` beta expects, so this may be
   smaller than it looks.
2. Deployment target: DebateWho's VPS/Traefik, new subdomain,
   `policy.hujson`/DNS entries — none of this exists yet and shouldn't
   be scaffolded speculatively in Phase 1.
3. Security scoping once tailnet-reachable: the real, enforced version
   of §7's `NETWORK_ALLOWLIST`. Default assumption is that this box
   reaches nothing besides `dev.debatewho.com`. Note this MUST be
   enforced at the network layer: `navigate`/`evaluate` give Chromium
   unfiltered egress that no tool-layer check can constrain (recorded
   in `docs/architecture/security.md`).
4. ~~The terminal~~ — **BUILT, see §15.** Struck 2026-07-27. Its own
   toggle `AGENTBOX_ENABLE_TERMINAL`, defaulting false, as decided here.
5. ~~Whether §8/§9's tools and the terminal actually get flipped off and
   verified off~~ — **DONE, PRD 1.9 satisfied.** Struck 2026-07-27.
   `tests/test_feature_toggle.py` starts a real container with
   `AGENTBOX_ENABLE_JUMPBOX_TOOLS=false` and asserts absence from
   `list_tools()`; `tests/test_terminal.py:166` asserts the route is
   absent, failing at the transport rather than returning a 403.
6. ~~The terminal's WebSocket reuses `AGENTBOX_AUTH_TOKEN` as `?token=`~~
   — **REVERSED, do not build this.** Struck 2026-07-27. RFC 9700
   §4.3.2 makes credentials in query strings a MUST NOT; they land in
   logs, proxies and `Referer`. The built terminal authenticates by
   `Authorization` header before `accept()`, or by a first text frame
   for browsers that cannot set headers, and a `?token=` parameter is
   *refused* rather than honoured. One secret is still reused — only
   the transport differs from Hill90.
7. **Bring `AGENTBOX_AUTH_TOKEN_FILE` into §12, then wire and test it.**
   `src/auth.py` implements this variable and the runbooks tell the
   operator to prefer it, but §12 never specified it — outside this item
   it appears nowhere in this document. `docker-compose.yml` also
   defines no `secrets:` block, so the preferred path is not usable as
   shipped, and no test covers it; the only `*_FILE` coverage is
   `tests/test_git_credentials.py`, for §16, which *does* pair with
   Compose `secrets:` and is the model to copy. Until this lands, the
   working configuration is the env-value form, which `docker inspect`
   returns verbatim. Phase 1 work, in that order — spec §12 first, then
   compose, then a test — and a prerequisite for every option in item 2.
8. **TLS termination before anything non-loopback.** Every surface is
   plain HTTP today. Correct while bound to `127.0.0.1`; a bearer token
   over cleartext is not, the moment item 2 makes this reachable.
9. **Exercise the rotation procedure once.** `AGENTBOX_AUTH_TOKEN_PREVIOUS`
   is covered at `tests/test_hardening.py:120`, but the five-step
   runbook in `docs/runbooks/rotate-auth-token.md` has never been run
   end to end. An untested rotation procedure is one that will not be
   used under pressure.
10. **DECISION — dependency pinning, and pip vs uv.** `pyproject.toml`
    declares every dependency as a floating minimum (`fastmcp>=2.0.0`,
    `playwright>=1.49.1`, and the rest), and there is no lockfile. So
    `docker compose build --no-cache` re-resolves them every time and can
    produce a materially different image with no signal that it did. This
    is the one unpinned surface in a repo that otherwise pins
    deliberately: the bookworm base, the tmux theme at `fcfde9a`, FiraCode
    at `v3.3.0`, Chromium via Playwright. hill90-app locks its equivalent
    service with `poetry.lock`.

    Tangled with it, but not the same question: the operator's standing
    preference (`$AGENT_MEMORY_VAULT`, `python-package-manager-uv`,
    recorded 2026-07-18) is uv, never pip or venv. `Dockerfile:129` uses
    `pip install -e .`, inherited from the initial commit of 2025-11-02 —
    it predates the preference rather than contradicting it.

    Adopting uv would deliver the lockfile and satisfy the preference in
    one move, at the cost of one more image dependency (`COPY --from` the
    uv image) in a repo that minimises them. Staying on pip and adding a
    lockfile another way is also viable.

    Not decided. Do not migrate the build on your own initiative. Lock
    from a known-green state when this is taken up — the suite was at
    257 pass / 3 skip when this was written.

## 15. Interactive terminal (PRD 1.11) — BUILT

> **Status: built and tested.** This section started as a draft with four
> open decisions; all four were resolved conservatively and implemented,
> and this now records what exists rather than what was proposed. What
> was chosen, and why, is called out at each point.

**Source has moved.** `hill90-app` now holds this code; the old
`/Users/jon/source/repos/Personal/Hill90` working tree was emptied when
the application was extracted on 2026-07-26 (see that repo's
`RESURRECTION.md`). Read-only reference, as before:

- `hill90-app/services/agentbox/app/ws_terminal.py` (211 lines) — the port target
- `hill90-app/services/ui/src/app/chat/XTerminal.tsx` (291 lines) — the UI model
- `hill90-app/services/agentbox/app/pty_shell.py` (158 lines) — **not** in scope, see below

Note that §2, §8 and §9 still point at the old path. Those references are
now dangling and want a sweep.

### 15.1 What to port

`ws_terminal_handler` as it stands. Concretely:

- **Auth first, before `websocket.accept()`.** The token comes from the
  `Authorization` header, or from a first text frame for browser clients
  that cannot set headers. A `?token=` query parameter is refused, not
  honoured (RFC 9700 §4.3.2). A mismatch closes with code `4001`, reason
  `unauthorized`. Rejecting before accepting means an unauthorised client
  never gets a live socket.
- **PTY setup:** `pty.openpty()`, `TIOCSWINSZ` to 120x40, `os.fork()`.
  The child calls `setsid()`, dups the slave onto fds 0/1/2, `chdir`s to
  `$HOME`, and `execvpe`s with an explicitly constructed environment —
  `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `TERM=xterm-256color`,
  `SHELL`. Nothing is inherited from the server process; keep it that way.
- **Shell resolution:** `tmux new-session -A -s agent -x 120 -y 40` when
  both tmux and zsh exist, else `zsh --login`, else `bash --login`. The
  slim base image has none of tmux or zsh, so today this lands on bash.
  Adding tmux is worthwhile — `-A` reattaches, so a dropped socket does
  not lose the session.
- **Relay:** parent sets `O_NONBLOCK` on the master fd, runs a reader
  task (PTY → WebSocket, `select` with a 100ms timeout in an executor,
  4096-byte reads, `send_bytes`) while the main loop does WebSocket →
  PTY via `os.write`.
- **Resize:** a JSON text frame `{"type":"resize","cols":N,"rows":N}`
  re-runs `TIOCSWINSZ` on the master and sends `SIGWINCH` to the child.
- **Teardown:** on disconnect, cancel the reader, close the master fd,
  `SIGTERM` the child, reap with `waitpid(..., WNOHANG)`.

Wire format, unchanged: binary frames are raw terminal I/O, text frames
are JSON control messages.

One quirk worth keeping: the Hill90 client sends `{"type":"ping"}`
keep-alives that the server does not understand. They fall into the
control handler's `except` and are ignored harmlessly. Port the
tolerance, not a new opinion about it.

### 15.2 Explicitly NOT in scope

`pty_shell.py` is a different thing — `execute_streaming()` runs one
argv in a PTY and yields output chunks, with a timeout that escalates
`SIGTERM` to `SIGKILL`. It is the engine for a streaming
`execute_command`, which PRD 1.2 rejected and SPEC §14 item 4 still
gates behind its own decision. The interactive terminal does not need
it. Do not port it as a bonus.

### 15.3 Toggle and auth

- `AGENTBOX_ENABLE_TERMINAL`, its own flag (§14 item 4), set explicitly
  in `docker-compose.yml`/`.env.example` like the others.
  **CHOSEN: defaults `false`**, even locally — every other toggle
  defaults on, but this one is a shell.
- When off, the `WebSocketRoute` is not registered. The trip-wire test
  is a connection attempt that fails at the transport, not a 403 from a
  live endpoint.
- Auth reuses `AGENTBOX_AUTH_TOKEN` via the `Authorization` header.
  `check_bearer()` takes a full header and delegates to
  `check_token(raw: str) -> bool`, which the WebSocket path also calls.
  Keep `secrets.compare_digest`.
- **CHOSEN: fail closed** when `AGENTBOX_AUTH_TOKEN` is unset, as Hill90
  does (`if not work_token or token != work_token`). This is the one
  place where "auth off means open" does not apply. The server logs a
  warning at startup if the terminal is enabled without a token, so the
  combination is never silently useless.

### 15.4 Route registration (verified)

`mcp.custom_route()` is HTTP-only — it builds a Starlette `Route` and
its docstring says the handler takes a `Request` and returns a
`Response`. WebSockets need a `WebSocketRoute`, and FastMCP's
`streamable_http_app()` does `routes.extend(self._custom_starlette_routes)`,
which is a plain list. Appending a `WebSocketRoute` to it therefore
works and keeps `mcp.run(transport="streamable-http")` intact.

Caveat: `_custom_starlette_routes` is private. Pin the behaviour with a
test that asserts the route is actually reachable, so an SDK bump that
renames it fails loudly rather than silently dropping the terminal.

### 15.5 UI

Mirror `XTerminal.tsx`'s model, not its React:

- xterm.js with the fit and web-links addons, `binaryType = "arraybuffer"`,
  `convertEol`, 5000-line scrollback.
- **Observing by default** (`disableStdin: true`), with a Take Control
  toggle that enables stdin and attaches `onData` → `ws.send(encoded)`.
  Status reads Controlling / Observing / Disconnected. This is the same
  two-state model the browser view uses; reuse the wording.
- On open, send the fitted dimensions as a resize; re-send on
  `ResizeObserver` fire. Keep-alive ping every 30s.
- Reconnect on close except codes `4001` (auth) and `1000` (clean), up
  to 5 attempts, backoff `min(2000 * n, 10000)`.
- **CHOSEN: vendored.** `src/vendor/` holds `xterm.js`, `xterm.css`,
  `xterm-addon-fit.js` and a 1KB Nerd Font subset, served by a
  `/vendor/{name}` route. A CDN would have broken both the no-build-step
  rule and the local-only property. See `src/vendor/README.md` for
  versions and how to refresh them.
- The browser and terminal are **tabs**, not a split pane — each view
  owns the full height. The terminal's WebSocket stays open while the
  browser tab is showing, so switching does not drop the session.

### 15.6 Container changes

- tmux and zsh are installed, so the shell is
  `tmux new-session -A -s agent` and a dropped socket reattaches.
- The tmux theme is `fabioluciano/tmux-tokyo-night` pinned to `fcfde9a`
  — the same commit and mods as hill90-app's agentbox — installed into
  the app user's home with TPM plugins pre-installed at build time, so
  the first session costs nothing and needs no network.
- `theme/zshrc` exists mainly so zsh does not run its first-run
  configuration wizard, which otherwise eats the opening keystrokes of
  every session. It carries a Tokyo Night prompt with no glyph
  dependency; Hill90's Powerlevel10k config is deliberately NOT ported,
  because p10k draws a large Nerd Font glyph set that the vendored
  1KB subset cannot cover and a CDN font is not an option.
- **CHOSEN: the container no longer runs as root.** A `agentbox` user
  (uid 1000) owns `/workspace`, the screenshots volume and the
  Playwright browser cache, and `scripts/entrypoint.sh` drops privileges
  with `setpriv --inh-caps=-all` after fixing volume ownership. The
  entrypoint exists rather than a bare `USER` directive because a named
  volume created by an earlier root-running build stays root-owned; the
  entrypoint makes the upgrade seamless. The PTY is therefore an
  unprivileged shell, matching Hill90's `agentuser`.

### 15.7 Tests

- Toggle off: the WebSocket connection fails; no route exists.
- Toggle on, no token / wrong token: closed with code `4001`, and
  nothing was accepted first.
- Toggle on, right token: connect, write `echo agentbox-test\n`, read
  the echoed output back — the real round-trip proof, equivalent to the
  browser suite's navigate-then-screenshot.
- Resize: send a resize frame, then confirm the child observed it (run
  `tput cols` in the shell and read the answer).
- Teardown: after the socket closes, no orphaned shell process remains —
  the same `docker top` technique `test_resilience.py` already uses for
  Chromium.
- Auth interaction: the terminal's gate is independent of the §12 HTTP
  gate; assert both, since they are different code paths.
- Privilege: the server process is not root, the shell is not root, the
  shell can still write `/workspace`, and no zsh wizard appears. These
  are what stop the non-root property regressing silently.

**Implemented in** `src/terminal.py`, `src/mcp_server.py` (route
registration and toggle), `src/ui.html` (panel), `theme/`,
`scripts/entrypoint.sh`. **Tested in** `tests/test_terminal.py`
(20 tests) and `tests/test_ui_api.py`.

## 16. Git push credentials (jumpbox tooling)

Read access needed nothing: `git clone`/`pull` over HTTPS already worked.
Push needs a credential, and a credential on this box can write to the
operator's source of truth — so it is opt-in and deliberately narrow.

- **Off by default.** With neither `AGENTBOX_GIT_CREDENTIALS_FILE` nor
  `AGENTBOX_GIT_SSH_KEY_FILE` set, git behaves exactly as before and the
  startup log says `Git push DISABLED`.
- **Files, not environment.** Both variables name a path. An environment
  value is returned verbatim by `docker inspect` — this repo's own audit
  demonstrated that leak for the auth token. Pair with Compose `secrets:`.
- **A read-only credential helper.** Git's built-in `store` helper also
  implements `store` and `erase`, so anything in the container could rewrite
  or delete the operator's secret through git. `agentbox-git-credential`
  answers `get` only, matches on protocol+host, and never opens the file for
  writing.
- **SSH keeps `StrictHostKeyChecking=yes`** and requires a `known_hosts`
  file. Turning it off is the usual shortcut and it converts a push
  credential into a machine-in-the-middle opportunity. With no host keys a
  push refuses to connect — the safe failure.
- **Scope belongs to the credential.** Use a fine-grained PAT limited to the
  repositories that need writing, or a per-repository deploy key. Nothing in
  this container can narrow a broad token.

Not solved, and inherent: with the terminal enabled the shell runs as the
same uid as the server, so it can read the credential file directly. Any
process that can run `git` can obtain the secret. The mitigations are the
scope of the credential and the terminal being off by default.

## 17. Jumpbox tooling

`openssh-client`, `less`, `procps`, `jq`, `vim`, `wget`, `unzip`, `ripgrep`,
`ca-certificates`, `iputils-ping`, `dnsutils`, `netcat-openbsd`, `rsync`.

Three of those are correctness rather than convenience: git pages through
`less`, an agent with no `ps` cannot see its own processes (`docker top` from
outside was the workaround), and a jumpbox that cannot `ssh` is a strange
jumpbox.
