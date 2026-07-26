# SPEC.md — AgentBox Phase 1 Engineering Spec

Build from this spec and `PRD.md` — not from conversation history or
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
(`/Users/jon/source/repos/Personal/Hill90/services/agentbox/app/tools.py`,
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
  is deferred, and why (§13 item 3 already tracks the real version of
  this for Phase 2).
- One test: `COMMAND_ALLOWLIST == []` and `NETWORK_ALLOWLIST == []` (or
  whatever documented baseline) as of this commit — a trivial assertion,
  but it's the trip-wire that makes a future accidental addition show
  up as an intentional diff instead of silent scope creep.

## 8. Filesystem and git tools (PRD 1.7)

Port, don't rewrite, from Hill90 (read-only reference —
`/Users/jon/source/repos/Personal/Hill90/services/agentbox/app/`):

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

## 12. Non-Functional Requirements

- Must run entirely locally via `docker-compose up` — no cloud
  dependency, no Tailscale, no auth layer. It is fine (expected) that
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

## 13. Open Items (Phase 2 — do not design or build yet)

1. OAuth wrapper approach (Cloudflare Worker `workers-oauth-provider`
   template vs. hand-rolled DCR/CIMD).
2. Deployment target: DebateWho's VPS/Traefik, new subdomain,
   `policy.hujson`/DNS entries — none of this exists yet and shouldn't
   be scaffolded speculatively in Phase 1.
3. Security scoping once tailnet-reachable: the real, enforced version
   of §7's `NETWORK_ALLOWLIST` — should this box reach anything besides
   `dev.debatewho.com`? Default assumption is no. This is where the
   Docker-level firewall/iptables enforcement deferred in §7 actually
   gets built, once there's a real deployment target to enforce it on.
4. The terminal (Hill90's `ws_terminal.py`/`pty_shell.py`/
   `XTerminal.tsx` — a real interactive PTY, not a policy-checked
   command tool). Its own dedicated build, its own PRD/SPEC entry when
   that starts, gated by §10's toggle framework from the moment it
   exists, and — same as §8/§9 — genuinely absent from the MCP/route
   surface when the toggle is off, not merely unlinked from the UI.
5. Whether §8/§9's tools, and the eventual terminal, actually get
   flipped off and verified off (PRD 1.9's last requirement) before
   Phase 2 planning starts — this needs to actually happen once, not
   just be assumed because a toggle exists.
6. For the terminal specifically, when it's built: carry over Hill90's
   own precedent of gating even local/dev access with a bearer token
   on the WebSocket (`?token=<WORK_TOKEN>`) — Hill90 doesn't expose it
   unauthenticated even to itself, and AgentBox shouldn't either.
