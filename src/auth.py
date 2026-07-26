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

import json
import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Read once at startup, like every other setting in this server.
AUTH_TOKEN = os.environ.get("AGENTBOX_AUTH_TOKEN", "").strip()

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


def check_token(token: str | None) -> bool:
    """Compare a bare token against AGENTBOX_AUTH_TOKEN.

    No configured token means deny — the same fail-closed rule Hill90's
    `_check_auth` applies. The comparison is constant-time; Hill90 uses
    `==`, and for a locally-reachable box the difference is academic,
    but there is no reason to hand out a timing oracle on a shared
    secret.

    This is the one comparison in the codebase. The HTTP path reaches it
    through `check_bearer` (which strips the header prefix first) and
    the terminal WebSocket reaches it directly with a `?token=` query
    param, exactly as Hill90 reuses WORK_TOKEN across both.
    """
    if not AUTH_TOKEN or not token:
        return False
    return secrets.compare_digest(token, AUTH_TOKEN)


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
        logger.info(
            "Auth ENABLED (AGENTBOX_AUTH_TOKEN): MCP tools and /api/* require "
            "a Bearer token; /health, /ui and /api/auth-required stay open"
        )
    else:
        logger.info("Auth DISABLED (no AGENTBOX_AUTH_TOKEN): all endpoints are open")
