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

### Out of Scope for Phase 1

- Any auth/OAuth layer.
- Any cloud or VPS deployment.
- Any Tailscale networking or configuration.
- Any DebateWho-specific configuration or code.
- The shell/exec tools from the original AgentBox.
- Hill90's non-browser tools (filesystem, git, `http_request`,
  knowledge/AKM, chat orchestration, WebSocket terminal). AgentBox
  Phase 1 is browser-only.

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
