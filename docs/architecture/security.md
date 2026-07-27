# Security model

Every control here exists for a stated reason, and most cite a standard.
This document is the place to look before changing anything that touches
auth, the terminal, or network exposure.

## Threat model

Phase 1 is **local Docker only, single operator**. The container is published
on `127.0.0.1` and nothing is exposed to the internet. That is the primary
control; everything else is defence in depth.

What makes this box unusual, and what drives most decisions below:

- It **runs a browser that reads untrusted web content**.
- It **grants a real shell** (PTY) when the terminal is enabled.

Those two together are the standard prompt-injection-to-RCE path. `docs/PRD.md`
§1.5 states this explicitly and is why a general `execute_command` tool was
rejected rather than merely postponed: binary allowlisting cannot contain a
real shell, because allowed binaries can be chained.

**Any auth bypass on this box is remote code execution.** Weight changes
accordingly.

## Layers

| Layer | Control | Default |
|---|---|---|
| Network | Published on `127.0.0.1` only | on |
| Network | `Origin` + `Host` allowlist (DNS-rebinding guard) | on |
| Identity | Shared bearer token on MCP tools and `/api/*` | **off** |
| Capability | `AGENTBOX_ENABLE_JUMPBOX_TOOLS` — filesystem/git/http | on |
| Capability | `AGENTBOX_ENABLE_TERMINAL` — the PTY | **off** |
| Scope | `PathPolicy` confines filesystem tools to `/workspace` | on |
| Scope | SSRF blocklist on `http_request` | on |
| Privilege | Runs as `agentbox` (uid 1000), not root | on |

## Auth

A shared bearer token, ported from hill90-app's `WORK_TOKEN` pattern.
Deliberately **not** OAuth — that is Phase 2 (`docs/SPEC.md` §14 item 1). The MCP
spec makes authorization OPTIONAL and its Security Best Practices page
explicitly sanctions *"Require an authorization token"* for locally-run HTTP
servers, so this is a conforming choice rather than a shortcut.

- Off by default. When `AGENTBOX_AUTH_TOKEN` is empty the guard is not wired
  in at all — not a branch that returns early, genuinely absent.
- One constant-time comparison (`secrets.compare_digest`) serves every
  surface. `check_bearer()` strips the header prefix and delegates to
  `check_token()`.
- **Prefer `AGENTBOX_AUTH_TOKEN_FILE`.** An environment variable is returned
  verbatim by `docker inspect`; with the file form the variable holds only a
  path. This is the Docker Official Images `*_FILE` convention. Pair it with
  a Compose `secrets:` block.
- **Rotation:** `AGENTBOX_AUTH_TOKEN_PREVIOUS` is accepted alongside the
  current token so clients can move across one at a time. Without an overlap
  window rotation needs coordinated downtime, so in practice it never
  happens.
- The startup log prints a SHA-256 fingerprint, never the secret.

Open on purpose: `GET /health` (the Docker healthcheck target — note the MCP
`health` *tool* is still gated), `GET /ui` (the page must load before it can
ask for a token), `GET /api/auth-required` (how the page learns whether to
prompt), `GET /vendor/*` (the page's own scripts).

## The terminal

Gated twice, and both gates are real.

1. `AGENTBOX_ENABLE_TERMINAL` must be on or the WebSocket route is never
   registered.
2. A valid token must be presented. This is **fail closed**: with no token
   configured the terminal refuses every connection, even though the rest of
   the server treats "no token" as "auth off". hill90-app does the same and
   never exposes this socket unauthenticated even to itself.

**Credentials never travel in the URL.** RFC 9700 (BCP 240, Jan 2025) §4.3.2
hardens RFC 6750's SHOULD NOT into *"Clients MUST NOT pass access tokens in a
URI query parameter"* — URLs reach access logs, proxy traces and browser
history, and the same secret guards the HTTP surface. Two paths instead:

- **Machine clients:** `Authorization: Bearer` on the handshake, checked
  *before* `accept()`, so they never get a socket. (Observable consequence:
  they see an HTTP 403 handshake rejection, not close code 4001.)
- **Browsers**, which cannot set WebSocket headers: the socket is accepted,
  the server sends `{"type":"auth_required"}`, and the client answers with
  `{"type":"auth","token":"..."}`.

A `?token=` parameter is **refused**, not honoured, so the old shape cannot
quietly persist.

The pre-auth window is the dangerous part. ttyd ≤1.3.0 authenticated its
handshake but not its receive path — a pre-auth RCE (NCC Group, 2017-09-08).
So: no PTY is spawned until auth succeeds, binary frames before auth close
the connection, text frames are size- and count-capped, and the window has a
deadline.

The shell's environment is built explicitly rather than inherited,
specifically so a session cannot read `AGENTBOX_AUTH_TOKEN` out of the server
process. There is a test.

## DNS-rebinding guard

Every request has `Origin` **and** `Host` checked against an allowlist;
mismatches get 403.

The MCP transports spec requires the `Origin` check. The `Host` check is the
one that actually stops DNS rebinding — an attacker's page resolves their own
hostname to `127.0.0.1`, so the packet arrives and `Origin` is theirs or
absent. This was a real CVE against Shellinabox (CVE-2015-8400), and it is
why **binding to loopback is not by itself a defence**.

Requests with no `Origin` are allowed: every non-browser client omits it, and
browsers always send one cross-origin. Substring-alike hostnames such as
`localhost.evil.com` are rejected — allowlist, not denylist.

## Filesystem scoping

`PathPolicy` (ported from hill90-app) confines the filesystem tools to
`/workspace`:

- Paths are `realpath`-resolved before checking, so `..` traversal and
  symlinks are judged on their target.
- Prefix comparison is against `allowed + "/"`, so `/workspaceless` is not a
  child of `/workspace`.
- **Default deny** — a path must match an allowed root.

## SSRF

`http_request` resolves the hostname and checks the **resolved IP** against a
blocklist: loopback, RFC1918, link-local (including cloud metadata at
169.254.169.254), and the 100.64.0.0/10 CGNAT range Tailscale uses. String
matching the URL would not catch a DNS name pointing inward.

Redirects are followed **manually, one hop at a time**, with every hop
re-checked. Letting httpx follow them would check only the URL we were
handed, so a public URL that 302s to the metadata service would sail through.
Non-HTTP redirect targets are refused; the chain is capped at 3.

`NETWORK_ALLOWLIST` in `policy.py` is where a future reviewed exception would
be checked — *before* the blocklist rejects it, never by removing an entry
from the blocklist.

## Privilege

The container runs as `agentbox` (uid 1000). `scripts/entrypoint.sh` is root
only long enough to fix volume ownership, then drops with
`setpriv --inh-caps=-all` so nothing the shell spawns can regain
capabilities. Tests assert the server process is not root, the shell is not
root, and the shell can still write `/workspace`.

## Audit findings — resolved

An adversarial audit confirmed each of these by demonstration. All are fixed
and re-verified against the original reproduction:

- Auth **failed open** when a configured token file was unreadable; now fatal
  at startup.
- SSRF blocklist missed `0.0.0.0` (reaches loopback on Linux) and every IPv6
  range, and resolved A records only. Replaced with `ipaddress` property
  checks over `AF_UNSPEC` results.
- **DNS-rebinding TOCTOU** — the check and the connection resolved DNS
  independently. `resolve_allowed()` now returns a vetted address and the
  request is **pinned** to that literal, with the hostname carried in `Host`
  and SNI. Applied to every redirect hop too.
- `write_file` could `mkdir -p` outside `/workspace`, because `makedirs`
  walked the unresolved path through `..`.
- The PTY leaked a zombie per session against `PidsLimit=200`; PTY readers
  also occupied asyncio's default executor, so enough sessions would wedge
  the HTTP surface.
- The shell could read the token from `/proc/1/environ`; it is now scrubbed
  from the environment once loaded.
- `initialize`/`tools/list` were answerable unauthenticated; `/mcp` is gated
  as a whole.
- `/ui` was framable; now `X-Frame-Options: DENY` plus a CSP.
- Privilege drop was incomplete — `NoNewPrivs=0` with the full bounding set
  and setuid-root binaries present. Now `--no-new-privs --bounding-set=-all`,
  plus `cap_drop: ALL` in Compose. Verified: `CapBnd: 0`, `NoNewPrivs: 1`.
- `http_request` buffered whole responses (OOM risk) — now capped while
  streaming. Pre-auth WebSockets are capped at 16 concurrent. Partial PTY
  writes were dropped. A timed-out browser dispatch left its coroutine
  running.

## Git push credentials

Off unless a credential file is configured; read access never needed one.
The secret is read from a **file** so it stays out of `docker inspect`, the
credential helper is **read-only** so nothing in the container can rewrite
it, and SSH keeps `StrictHostKeyChecking` on. Scope the credential itself —
a fine-grained PAT limited to specific repositories, or a per-repo deploy
key. See `docs/SPEC.md` §16.

**Inherent limitation:** with the terminal on, the shell shares the server's
uid and can read the credential file. Any process that can run `git` can get
the secret; that is what scoping the credential is for.

## Known limitation: the browser is a second egress path

`http_request` is SSRF-filtered. **`navigate` and `evaluate` are not.** An
authenticated caller can point the browser at `http://169.254.169.254/` and
read it back with `get_text`, and `evaluate` runs arbitrary JavaScript that
can `fetch()` anything the container can reach.

This is deliberate, not an oversight: the browser's whole purpose is
navigating to arbitrary URLs, and a future goal is browser-verifying an
internal host. But it means the SSRF controls constrain one tool and not the
box, and anyone reasoning about egress should know that.

## Known limitations, accepted for Phase 1

- **One shared page, one shared workspace, one shared token.** Single
  operator by design. No per-user identity.
- **The viewer keeps its token in `sessionStorage`.** OWASP ASVS 7.2.2 would
  prefer a dynamically-generated session token; an `HttpOnly` cookie session
  is the known upgrade and is deliberately not built while this is
  single-operator on loopback. Note it would not fix XSS — it downgrades
  "token exfiltrated, usable anywhere forever" to "attacker acts through the
  victim's browser while the page is open".
- **No expiry, no revocation without a restart, no scope.** Rotation overlap
  is the mitigation.
- **No audit log of terminal commands.** NIST SP 800-53 AC-17(4) would want
  "assessable evidence" for privileged remote commands.
- **Concurrent `navigate` calls** to the one shared page make Chromium abort
  the superseded ones with `ERR_ABORTED`. Normal single-page behaviour; each
  caller gets a clean error.

## Phase 2 is not built

No OAuth, no cloud deploy, no Tailscale, no DebateWho config, no
`execute_command`. `docs/SPEC.md` §14 tracks these. Note two findings that will
matter when it starts:

- **claude.ai cannot reach a localhost server at all** — it rejects any
  hostname resolving to a private, loopback or CGNAT address before any HTTP
  request leaves Anthropic's network. Exposure is a prerequisite for that
  route, not an afterthought. Claude Code connects from your machine, so it
  is unaffected.
- **OAuth here would not require Dynamic Client Registration.** A
  pre-registered client ID is supported and is the recommended pattern for a
  single organisation, which makes it a smaller job than it first appears.
