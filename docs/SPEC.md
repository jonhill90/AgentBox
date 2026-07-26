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

## 5. Tool surface (exact list — nothing beyond this)

`navigate`, `screenshot`, `click` (by selector), `get_text`,
`evaluate` (JS), `click_at_percent`, `type_at`, `press_key`, `scroll`,
`history` (back/forward/reload), `health`.

Explicitly not in scope: `execute_command`, `manage_process`,
filesystem tools, git tools, `http_request`, knowledge/AKM tools. If
you find yourself wanting to add one of these to make something
easier, stop and flag it instead — it's an out-of-scope decision, not
a Phase 1 implementation detail.

## 6. Non-Functional Requirements

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
- Update `README.md` to describe the new tool set and drop the
  `execute_command`/`manage_process` documentation.

## 7. Open Items (Phase 2 — do not design or build yet)

1. OAuth wrapper approach (Cloudflare Worker `workers-oauth-provider`
   template vs. hand-rolled DCR/CIMD).
2. Deployment target: DebateWho's VPS/Traefik, new subdomain,
   `policy.hujson`/DNS entries — none of this exists yet and shouldn't
   be scaffolded speculatively in Phase 1.
3. Security scoping once tailnet-reachable: should this box reach
   anything besides `dev.debatewho.com`? Default assumption is no.
4. Whether `execute_command` ever gets added back, and if so, with a
   policy at least as strict as Hill90's `CommandPolicy`
   (`shell=False`, resolved-path allowlist, scrubbed env) — never the
   original unrestricted `shell=True` version from this repo's first
   commit.
