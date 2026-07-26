# PRD.md — AgentBox

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
  `SPEC.md` §5 for the exact list. Nothing beyond it.
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

### Out of Scope for Phase 1

- Any auth/OAuth layer (beyond a bearer-token gate on the terminal
  itself, ported from Hill90 — see the terminal's own future PRD entry).
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
