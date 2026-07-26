#!/usr/bin/env python3
"""
Local auth layer (PRD 1.10 / SPEC §12) — one shared secret, checked as a
Bearer token.

Ported from Hill90's `WORK_TOKEN` pattern (read-only reference:
`services/agentbox/app/runtime.py::_check_auth`, and `server.py`'s
`os.environ.get("WORK_TOKEN")`). Deliberately **not** OAuth: that is
Phase 2's job (SPEC §14 item 1). This is the simple thing a single local
operator needs today, and it is the credential an OAuth wrapper — or
Anthropic's `static_headers` connector auth — sits in front of later, so
it is reusable rather than throwaway.

Off by default. `AGENTBOX_AUTH_TOKEN` unset or empty means no auth, and
callers must not wire the check in at all in that case, so behaviour is
byte-identical to a build without this module.

What is gated when it *is* set: every MCP tool, and every `/api/*` route
behind the viewer. Deliberately not gated:

  - `GET /health`         — the Docker healthcheck target, not a
                            capability. Hill90 leaves it open too.
  - `GET /ui`             — the page has to load before it can ask for a
                            token; it exposes no browser state itself.
  - `GET /api/auth-required` — how the page learns whether to prompt.
                            Answering "is a token needed here" to an
                            unauthenticated caller tells them nothing
                            they could not learn by getting a 401.

The same token is what the terminal's WebSocket will check as
`?token=` when it exists (SPEC §14 item 6) — one secret, not two
mechanisms.
"""

import hashlib
import json
import logging
import os
import pathlib
import secrets

logger = logging.getLogger(__name__)

def _read_secret(name: str) -> str:
    """Read a secret from `<NAME>_FILE` if set, else from `<NAME>`.

    The `*_FILE` convention is the Docker Official Images idiom (postgres,
    mysql, mariadb all use it) and is what Docker's own Compose docs point
    at instead of environment variables: "If you're injecting passwords and
    API keys as environment variables, you risk unintentional information
    exposure."

    That risk is not hypothetical here — an env var is returned verbatim in
    `docker inspect` output to anyone with Docker socket access. With the
    file form the env var holds only a path, which is harmless to leak.
    Pair it with a Compose `secrets:` block, which mounts the value at
    /run/secrets/<name>.
    """
    path = os.environ.get(f"{name}_FILE", "").strip()
    if path:
        try:
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.error(f"Cannot read {name}_FILE at {path}: {exc}")
            return ""
    return os.environ.get(name, "").strip()


# Read once at startup, like every other setting in this server.
AUTH_TOKEN = _read_secret("AGENTBOX_AUTH_TOKEN")

# A second, simultaneously-valid token, so rotation is a non-event.
#
# Without an overlap window, rotating means downtime plus coordinated
# reconfiguration of every client, so in practice it never happens and the
# "rotation policy" stays theoretical. Set PREVIOUS to the outgoing value,
# move clients across one at a time, then clear it. Both are accepted while
# both are set. OWASP: "You should regularly rotate secrets so that any
# stolen credentials will only work for a short time."
AUTH_TOKEN_PREVIOUS = _read_secret("AGENTBOX_AUTH_TOKEN_PREVIOUS")

UNAUTHORIZED_ERROR = "Unauthorized: missing or invalid Bearer token"

# What an MCP tool returns when the caller is not authorised. Tools
# always answer with a JSON string, so this keeps that contract — the
# `status` field carries the 401 that the transport cannot.
UNAUTHORIZED_TOOL_RESULT = json.dumps({
    "success": False,
    "status": 401,
    "error": UNAUTHORIZED_ERROR,
})

# Body of a REST 401. Same shape, sent with an actual HTTP 401.
UNAUTHORIZED_BODY = {"success": False, "status": 401, "error": UNAUTHORIZED_ERROR}


def auth_enabled() -> bool:
    """True when a token is configured. When False, nothing should be gated."""
    return bool(AUTH_TOKEN)


def token_fingerprint(token: str) -> str:
    """A short SHA-256 prefix, safe to log — tells you WHICH token was used
    without the log itself becoming a credential store."""
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else "-"


def check_token(token: str | None) -> bool:
    """Compare a bare token against AGENTBOX_AUTH_TOKEN.

    No configured token means deny — the same fail-closed rule Hill90's
    `_check_auth` applies. The comparison is constant-time; Hill90 uses
    `==`, and for a locally-reachable box the difference is academic,
    but there is no reason to hand out a timing oracle on a shared
    secret.

    This is the one comparison in the codebase. The HTTP path reaches it
    through `check_bearer` (which strips the header prefix first) and
    the terminal WebSocket reaches it directly, either from an
    Authorization header or from its post-connect auth message.
    """
    if not AUTH_TOKEN or not token:
        return False
    # Both compared unconditionally — no short-circuit, so the number of
    # comparisons does not depend on which token matched.
    current = secrets.compare_digest(token, AUTH_TOKEN)
    previous = bool(AUTH_TOKEN_PREVIOUS) and secrets.compare_digest(token, AUTH_TOKEN_PREVIOUS)
    return current or previous


def check_bearer(authorization: str | None) -> bool:
    """Validate an Authorization header against AGENTBOX_AUTH_TOKEN.

    Mirrors Hill90's `_check_auth`: no configured token means deny,
    the `Bearer ` prefix is required, and the remainder must match.
    """
    if not authorization or not authorization.startswith("Bearer "):
        return False

    return check_token(authorization[7:])  # len("Bearer ") == 7


def authorization_from_mcp_request() -> str | None:
    """Pull the Authorization header off the in-flight MCP request.

    FastMCP hands the low-level server the underlying HTTP request, which
    it stashes on the per-request context var. Written defensively: if
    the shape ever changes, this returns None and the caller denies,
    which fails closed rather than open.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
    except (ImportError, LookupError):
        return None

    request = getattr(ctx, "request", None)
    if request is None:
        return None

    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            return headers.get("authorization")
        except (AttributeError, TypeError):
            pass

    # Fall back to a raw ASGI scope.
    if isinstance(request, dict):
        for key, value in request.get("headers", []):
            if key.lower() == b"authorization":
                return value.decode("latin-1")

    return None


def mcp_request_authorized() -> bool:
    """True if the in-flight MCP request carries a valid Bearer token."""
    return check_bearer(authorization_from_mcp_request())


def log_startup_state() -> None:
    if auth_enabled():
        source = "file" if os.environ.get("AGENTBOX_AUTH_TOKEN_FILE") else "env"
        extra = " (+1 previous token accepted for rotation)" if AUTH_TOKEN_PREVIOUS else ""
        logger.info(
            f"Auth ENABLED (from {source}, fingerprint {token_fingerprint(AUTH_TOKEN)})"
            f"{extra}: MCP tools and /api/* require a Bearer token; "
            "/health, /ui and /api/auth-required stay open"
        )
    else:
        logger.info("Auth DISABLED (no AGENTBOX_AUTH_TOKEN): all endpoints are open")
