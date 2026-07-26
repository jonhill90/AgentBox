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

Those two together are the standard prompt-injection-to-RCE path. `PRD.md`
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
Deliberately **not** OAuth — that is Phase 2 (`SPEC.md` §14 item 1). The MCP
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

The container runs as `agentbox` (uid 1000). `docker-entrypoint.sh` is root
only long enough to fix volume ownership, then drops with
`setpriv --inh-caps=-all` so nothing the shell spawns can regain
capabilities. Tests assert the server process is not root, the shell is not
root, and the shell can still write `/workspace`.

## Open findings from the security audit

Found by an adversarial audit, confirmed by demonstration, **not yet fixed**:

- **DNS-rebinding TOCTOU in `http_request`.** `is_blocked_host` resolves the
  name, then httpx resolves it again independently. A TTL-0 record answering
  public once and private the second time gets through. The complete fix is
  to resolve once and pin the connection to the vetted address, carrying the
  hostname in `Host`/SNI. The blocklist itself is now correct; this is the
  gap between checking and connecting.
- **`navigate` + `evaluate` are a second, unguarded egress path.** The SSRF
  blocklist applies only to `http_request`. The browser can be navigated to
  `http://169.254.169.254/` and its content read back with `get_text`, and
  `evaluate` runs arbitrary JavaScript which can `fetch()` anything. Any
  authenticated caller has this. It is not a bypass of a control — no control
  was ever applied there — but the security story should say so plainly.
- **`http_request` buffers the whole response before truncating**, so a
  hostile endpoint returning a multi-GB body can OOM the container.
- **No cap on concurrent pre-auth WebSocket sockets.** Each is bounded to 10s,
  so this is availability-only.

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
`execute_command`. `SPEC.md` §14 tracks these. Note two findings that will
matter when it starts:

- **claude.ai cannot reach a localhost server at all** — it rejects any
  hostname resolving to a private, loopback or CGNAT address before any HTTP
  request leaves Anthropic's network. Exposure is a prerequisite for that
  route, not an afterthought. Claude Code connects from your machine, so it
  is unaffected.
- **OAuth here would not require Dynamic Client Registration.** A
  pre-registered client ID is supported and is the recommended pattern for a
  single organisation, which makes it a smaller job than it first appears.
