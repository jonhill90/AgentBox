# docs/PRD.md — AgentBox

> **Status:** Phase 1 — local browser-tool MCP server, Docker-only, dev
> machine only. Nothing in this phase touches a cloud host, a tailnet,
> or DebateWho.

## Vision

AgentBox is a self-hosted MCP server that gives an AI agent a real,
persistent browser it can drive — and lets a human take the wheel on
that same live session. It exists to close a specific gap: Claude Code
sessions running in Anthropic's cloud sandbox (claude.ai/code) have no
browser today and cannot verify anything they build against a real
page — they can only claim success from logs or API responses.
AgentBox is the browser Claude Code Web is missing.

Two consumers, in order — **only the first is in scope for this PRD**:

1. **Phase 1 (this document): local dev, any MCP client.** Prove the
   server actually works, running entirely on the operator's machine,
   before it is exposed anywhere.
2. **Phase 2 (future, separate PRD amendment): DebateWho's Claude Code
   Web sessions.** Once Phase 1 is proven, adopt this as the
   implementation for DebateWho's DW-71 — hosted on DebateWho's own
   VPS/tailnet so it can browser-verify `dev.debatewho.com` per that
   project's CLAUDE.md "browser-verify before any fixed claim" rule.
   Do not design for this yet; it is out of scope until Phase 1 works.

## Phase 1 Requirements

### 1.1 Browser tool over MCP

**User story:** As an MCP client (Claude Code, the MCP inspector, or
any Streamable-HTTP-capable client), I can connect to AgentBox and
drive a real Chromium browser — navigate, screenshot, click, read
text, run JS, and click/type/scroll by coordinate — across multiple
tool calls without the page resetting between them.

**Requirements:**

- Ported from Hill90's `services/agentbox/app/tools.py`
  persistent-browser-loop pattern: one long-lived asyncio event loop
  in a background thread owns the single Playwright `Page` for the
  life of the process. Tool calls dispatch into that loop; the page
  is never recreated per call.
- Tool surface is a strict subset of Hill90's browser tool — see
  `docs/SPEC.md` §5 for the exact list. Nothing beyond it.
- Transport: Streamable HTTP, matching this repo's existing
  `src/mcp_server.py` (FastMCP) pattern.
- Runs in Docker via `docker-compose`, on the operator's machine only.

### 1.2 No general shell/exec tool

**Out of scope for Phase 1**, and not a given for Phase 2 either:
`execute_command` / `manage_process`, carried over from this repo's
original version. A browser-only box has a much smaller blast radius
than a shell-exec box — especially once (Phase 2) it might sit on a
tailnet next to production infrastructure. Do not port these tools
without a deliberate, separate decision when Phase 2 is scoped.

### 1.3 Local verification loop

**User story:** As the operator, I can prove the server actually works
— not just that it imports cleanly — before trusting it with anything
real.

**Requirements:**

- `docker-compose up` brings the server up locally.
- A documented, repeatable test (integration test or a throwaway MCP
  client script) connects, navigates to a real page, takes a
  screenshot, and reads it back — proving state persists across calls.
- No authentication in this phase. The server is not exposed beyond
  the operator's local Docker network.

### 1.4 Take-control viewer UI

**User story:** As the operator, I want to actually watch and drive the
browser AgentBox is controlling — not just read test output — so I can
see what it's doing and decide what to build next by looking at it.

**Requirements:**

- Mirrors Hill90's `services/ui/src/app/chat/SessionPane.tsx`
  `BrowserView` pattern exactly (read-only reference; do not modify
  Hill90): a screenshot viewer polling roughly every 2 seconds, a
  chrome bar with back/forward/reload and a URL input that navigates,
  and a **Take Control** toggle. When Take Control is on, clicking the
  screenshot image sends a coordinate click to the real page, scrolling
  the image sends a scroll, and typing while it's focused sends
  keystrokes — all against the same persistent page AgentBox already
  drives, not a separate session.
- Served locally, still no auth (matches PRD 1.3) — this is a dev tool
  for the operator, not exposed anywhere.
- "Describe" element-picker mode (Hill90's other toggle) is a nice to
  have, not required for this to be usable — build Take Control +
  screenshot + nav chrome first; add Describe only if it's quick.

### 1.5 Jumpbox allowlist scaffolding (mechanism only — grants nothing yet)

**User story:** As the operator, I want AgentBox to be able to gain new,
narrow capabilities over time (e.g. reading a specific log source, or
reaching one specific internal host once this is on DebateWho's VPS)
without ever re-opening its security model to do it — each addition
should be a small, reviewable, reversible change, not a rebuild.

**Why this exists:** the eventual purpose of this box (once deployed,
Phase 2) is a "jumpbox" the agent uses because it has no other way to
reach a network Claude Code Web sessions can't otherwise touch — that
is a deliberate, security-first design, not a general-purpose dev
shell. A full interactive shell/PTY is explicitly rejected for the
agent-facing case: it cannot be contained by command-allowlisting
alone (once a real shell is available, allowlisting individual
binaries doesn't stop chaining them), and an agent that also reads
untrusted content from the open web (its own browser tool) combined
with any shell access is a standard prompt-injection-to-RCE path.
Nothing here builds that.

**Requirements:**

- A command allowlist as **data, not code** — an explicit, empty (or
  near-empty) config listing exactly which resolved-absolute-path
  binaries and argument shapes are ever permitted, in the same spirit
  as Hill90's `CommandPolicy` (`shell=False`, resolved paths, scrubbed
  env — never the unrestricted `shell=True` this repo's first commit
  had). Empty today; adding one entry later is a one-line, reviewed
  change.
- A network-egress allowlist as **data, not code** — an explicit,
  empty (or near-empty) list of hosts this container may reach beyond
  localhost, enforced at the Docker network layer (not just checked in
  application code — app-level checks alone don't contain a real
  shell the way they can contain a structured HTTP tool). Empty today
  (this box currently only needs to reach whatever URL an agent
  navigates it to over the open internet); a future host (e.g.
  `dev.debatewho.com`, once Phase 2 exists) is again a one-line,
  reviewed addition, not a redesign.
- No shell tool, no PTY, no `execute_command` is added by this
  requirement. This is purely the extension mechanism, wired in but
  unused, so that when a specific real need shows up later (a log
  viewer, a specific internal host) it can be granted narrowly instead
  of reached for broadly.

### 1.6 Revised direction: full Hill90 tool parity minus AKM

**Decision (supersedes the "browser-only" framing above):** AgentBox
should become a full-featured, self-contained agent sandbox — everything
Hill90's `services/agentbox` has, minus the two knowledge/AKM tools,
which are permanently out of scope (AKM is a Hill90-specific concept —
a shared Postgres+pgvector knowledge base tied to Hill90's own
multi-agent platform — and will never be added here). Everything else
(filesystem, git, `http_request`, and a real interactive terminal) is
in scope, so this can be "a full-featured thing usable with any
harness to help with vision," not just a browser.

**The reason this is safe to do now, when it wasn't safe to wave through
casually earlier:** every tool added under this heading is built and
run in a fully local, unauthenticated, single-operator Docker
environment — nobody but the operator can reach it. The risk that
justified excluding a full shell earlier (an OAuth-exposed, publicly
reachable agent that also reads untrusted web content having
unrestricted execution next to production infrastructure) is a Phase 2
risk, not a Phase 1 one. Building broad capability now, in a
context where it's safe to build, and gating it hard before it is ever
exposed, is the right sequencing — see 1.9.

### 1.7 Filesystem and git tools

**User story:** As an agent (or the operator, through the same tools),
I can read/write files and make git commits inside a scoped workspace
directory, the same way Hill90's agent can.

**Requirements:**

- Ported from Hill90's `services/agentbox/app/filesystem.py`
  (`read_file`, `write_file`, `list_directory`, all `PathPolicy`-gated
  — realpath-resolved allow/deny, explicit read-only mode) and the
  `git` tool in `services/agentbox/app/tools.py` (`_execute_git`),
  which is itself already structured, not a raw shell: a fixed set of
  subcommands (`init`, `status`, `add`, `commit`, `diff`, `log`,
  `reset`) scoped to one workspace directory — port it as that same
  fixed subcommand set, not as arbitrary `git <anything>`.
- Both are read-only reference from Hill90; port the logic, don't
  modify that repo.

### 1.8 `http_request` tool (SSRF-protected)

**User story:** As an agent, I can make outbound HTTP requests to
external APIs, the way Hill90's agent can — without being able to pivot
into internal/private networks by doing so.

**Requirements:**

- Ported from Hill90's `_execute_http_request` / `_is_blocked_host`:
  blocks loopback, RFC1918, link-local, and — notably — the Tailscale
  CGNAT range (`100.64.0.0/10`), by resolving the hostname and checking
  the resolved IP against the blocklist, not just string-matching the
  URL.
- This tool's future host exceptions (e.g. eventually allowing
  `dev.debatewho.com` specifically once Phase 2 exists) go through
  `NETWORK_ALLOWLIST` from 1.5 — the blocklist is the permanent
  default-deny baseline; the allowlist is where specific, reviewed
  exceptions get added later, never by loosening the blocklist itself.

### 1.9 Feature-toggle framework (governs 1.7, 1.8, and the terminal)

**User story:** As the operator, I want to build full capability now
and be confident it is genuinely off — not just unlinked from the UI —
in any profile other than local dev, before this ever gets near
DebateWho's infrastructure.

**Requirements:**

- Every tool added under 1.7, 1.8, and the eventual terminal is gated
  by an explicit, environment-driven flag (e.g.
  `AGENTBOX_ENABLE_JUMPBOX_TOOLS=true`), read once at server startup.
  When a flag is off, the corresponding MCP tool(s) and REST route(s)
  are **not registered at all** — not hidden, not just unlinked from
  the `/ui` page. This must be enforced by the server, not by
  convention.
- Default: **on** for local `docker-compose` dev (matches this
  project's current phase). The moment any deployment profile besides
  local dev exists (Phase 2), that profile's default must be **off**,
  and turning any of these on for a non-local profile is its own
  reviewed decision, not inherited automatically.
- A trip-wire test per flag: with the flag off, assert the tool/route
  is genuinely absent (a 404 or a `list_tools()` surface that doesn't
  include it) — not just "the UI doesn't show a button for it."
- Before Phase 2 planning starts in earnest, actually flip every one of
  these flags off once, prove the server still starts and the browser
  tool/UI still work, and only then treat "off for DebateWho" as a
  verified fact rather than an assumption.

### 1.10 Local auth layer

**User story:** As the operator, I want to figure out authentication
against AgentBox itself, locally, before any OAuth/cloud discussion —
so that by the time DebateWho integration is on the table, auth is a
solved, tested problem being reused, not a new one being designed under
pressure.

**Why now, and why this shape:** Hill90 already has the answer for its
own agentbox — a single shared secret (`WORK_TOKEN`), checked as a
Bearer header on its `/work` endpoint and as a `?token=` query param on
its WebSocket terminal. That's a real, working precedent, not a
hypothetical — port the pattern rather than designing a new one. This
is deliberately *not* the OAuth 2.1/DCR flow Phase 2 will eventually
need for claude.ai custom connectors (see SPEC §13) — it's the simpler
shared-secret layer that makes sense for a single local operator today,
and it also happens to be exactly the shape Anthropic's `static_headers`
connector auth (currently in beta) expects, so it isn't wasted work
even once Phase 2 arrives: it becomes the credential an OAuth wrapper
or a header-based connector sits in front of, not something thrown away.

**Requirements:**

- One env var (e.g. `AGENTBOX_AUTH_TOKEN`), optional. Empty/unset means
  auth is off — today's behavior is unchanged by default.
- When set, it gates: every MCP tool call, and every `/api/*` REST
  route from the take-control UI. `/health` stays unauthenticated
  (matches Hill90 — it's a Docker healthcheck target, not a capability).
- Checked as `Authorization: Bearer <token>` on MCP/REST requests.
  When the terminal exists (a later, separate build), its WebSocket
  reuses this same token as a `?token=` query param, exactly like
  Hill90's `ws_terminal_handler` — one secret, not a second mechanism.
- The `/ui` page prompts for the token once (if the server reports
  auth is enabled) and stores it for the session (e.g.
  `sessionStorage`), attaching it to every `/api/*` call afterward.
  Don't require re-entering it on every request.
- Missing/wrong token: a clear 401 with a structured error, not a
  silent failure or a raw exception.

### 1.11 Interactive terminal — BUILT

> **Status: built and tested.** Drafted with four open decisions; all
> four were resolved conservatively and implemented. See SPEC §15 for
> what exists and `tests/test_terminal.py` for what is proven.

**User story:** As the operator, I want a real shell inside AgentBox that
I can watch and type into from the same `/ui` page that already shows me
the browser — so the box is a place I can actually work, not only a set
of structured tools an agent calls.

**Why this is last, and why it gets its own toggle:** a PTY is the one
capability PRD 1.5 argues cannot be contained by allowlisting — once a
real shell exists, permitted binaries can be chained, and an agent that
also reads untrusted web content through its own browser tool has a
standard prompt-injection-to-RCE path. Everything else in Phase 1 was
built so that this piece can be added deliberately, gated hard, and
turned off as a single reviewed switch. It is not a general loosening of
the box; it is one more capability behind one more flag.

**Requirements:**

- Ported from `hill90-app`'s `services/agentbox/app/ws_terminal.py`
  (read-only reference): a PTY spawned per WebSocket connection,
  bidirectional relay, binary frames for terminal I/O and JSON text
  frames for control, plus `SIGWINCH`-backed resize.
- Its own toggle, `AGENTBOX_ENABLE_TERMINAL`, never shared with
  `AGENTBOX_ENABLE_JUMPBOX_TOOLS` (SPEC §14 item 4). When off, the
  WebSocket route is not registered at all — a connection attempt gets a
  transport-level rejection, not a polite refusal from a live endpoint.
  Same discipline as §8/§9's tools.
- Authenticated with the **same** `AGENTBOX_AUTH_TOKEN` from 1.10, as a
  `?token=` query param, exactly as Hill90's `ws_terminal_handler` reuses
  `WORK_TOKEN`. One secret, not a second mechanism.
- **Fail closed when no token is set.** Hill90 refuses the socket
  outright when `WORK_TOKEN` is unset, so it is never exposed
  unauthenticated even to itself (SPEC §14 item 6). AgentBox mirrors
  that: the terminal does not work until `AGENTBOX_AUTH_TOKEN` is set,
  even locally, and the server warns at startup if it is enabled
  without one.
- **Default off, even for local dev.** Every other toggle defaults on
  locally. This one is a shell, so turning it on is always an explicit
  act.
- **The shell is not root.** The container runs as an `agentbox` user
  (uid 1000) that owns the workspace, the screenshots volume and the
  browser cache; an entrypoint fixes volume ownership and then drops
  privileges. This matches Hill90's `agentuser` and means the PTY hands
  out an unprivileged shell.
- An xterm.js panel in `/ui`, alongside the browser view, mirroring
  `XTerminal.tsx`: read-only "Observing" by default, with a Take
  Control toggle that enables stdin — the same two-state model the
  browser view already uses, so the page has one consistent idea of what
  "taking control" means.

### Out of Scope for Phase 1

- OAuth/DCR (that's Phase 2, SPEC §13 — this section's bearer token is
  a different, simpler mechanism, not a precursor implementation of it).
- Any cloud or VPS deployment.
- Any Tailscale networking or configuration.
- Any DebateWho-specific configuration or code.
- AKM/knowledge tools — permanently out of scope, not deferred.
- The terminal itself is scoped separately (its own PRD/SPEC entry, a
  dedicated follow-up build) rather than bundled into 1.7/1.8, since
  it's the highest-risk single piece and deserves its own focused pass.

## Phase 2 (future — do not build yet)

Recorded here only so Phase 1 work doesn't foreclose it by accident:

- An OAuth wrapper in front (e.g. a Cloudflare Worker using the
  `workers-oauth-provider` template), so the server is addable as a
  claude.ai custom connector.
- Deployment to DebateWho's VPS (same tailnet as `dev.debatewho.com`),
  fronted by Traefik.
- A security review of what the box can reach once it's
  tailnet-attached, given it would sit next to DebateWho production.
- Formal adoption as the implementation for DebateWho's DW-71.
