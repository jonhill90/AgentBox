# 0003 — The terminal defaults off, and requires auth

**Status:** accepted.

## Decision

`AGENTBOX_ENABLE_TERMINAL` is its own flag, separate from the jumpbox
tools, and it is the only toggle that defaults **off**. The WebSocket is
fail-closed: with no `AGENTBOX_AUTH_TOKEN` configured it refuses every
connection with close code 4001 rather than serving an unauthenticated
shell.

## Why it is not just another tool

The structured tools are bounded — the filesystem tools are path-scoped,
git takes a fixed set of subcommands, `http_request` resolves and pins its
destination. A shell is none of those things. Combined with the browser
reading untrusted web content, it completes a prompt-injection-to-RCE path
(`docs/PRD.md` §1.5). That is a different category of exposure and gets a
different default.

## Rejected: folding it into the jumpbox toggle

One flag would mean enabling the file tools silently enables a shell.
The blast radii are not comparable, so the decisions should not be either.

## Consequences

- Two flags to set for a full jumpbox. Deliberate friction.
- Auth is a hard prerequisite, not a recommendation.
- Tokens never travel in the WebSocket URL (RFC 9700 §4.3.2 MUST NOT):
  header clients authenticate before `accept()`, browsers send an auth
  message after connecting.

**Detail:** [`../architecture/security.md`](../architecture/security.md)
§ The terminal. **Procedure:** [`../runbooks/enable-terminal.md`](../runbooks/enable-terminal.md).
