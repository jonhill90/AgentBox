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

### Out of Scope for Phase 1

- Any auth/OAuth layer.
- Any cloud or VPS deployment.
- Any Tailscale networking or configuration.
- Any DebateWho-specific configuration or code.
- Any actual shell/exec tool, and any interactive PTY/terminal (Hill90's
  `ws_terminal.py`/`pty_shell.py`/`XTerminal.tsx` pattern) — not ruled
  out forever, but never added casually alongside something else; each
  would need its own deliberate scoping decision the way this section
  itself was one.
- Hill90's non-browser tools (filesystem, git, `http_request`,
  knowledge/AKM, chat orchestration). AgentBox Phase 1 is browser-only,
  plus the empty allowlist scaffolding in 1.5.

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
