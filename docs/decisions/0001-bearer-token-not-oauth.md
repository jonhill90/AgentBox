# 0001 — A shared bearer token, not OAuth

**Status:** accepted, Phase 1.

## Decision

Authenticate every MCP tool and `/api/*` route with a single shared bearer
token, compared in constant time. Off by default: when no token is
configured the guard is not wired in at all.

## Rejected: OAuth 2.1

OAuth is `docs/SPEC.md` §14 item 1 — explicitly Phase 2, explicitly not built.
For a loopback-bound single-operator server it would add an authorization
server, redirect handling, and token lifecycle for no gain in this threat
model.

## Why this is conforming, not a shortcut

The MCP specification makes authorization OPTIONAL for HTTP transports, and
its Security Best Practices page sanctions *"Require an authorization
token"* for locally-run servers by name. This is the sanctioned path.

## Consequences

- One secret to hold, so rotation needs an overlap window — see
  [`0004-secrets-from-files.md`](0004-secrets-from-files.md) and the
  [rotation runbook](../runbooks/rotate-auth-token.md).
- No per-client identity or revocation. Acceptable for one operator; it is
  the first thing OAuth would buy.
- Four routes stay open on purpose (`/health`, `/ui`, `/api/auth-required`,
  `/vendor/*`) — the page must load before it can ask for a token.

**Detail:** [`../architecture/security.md`](../architecture/security.md) § Auth.
