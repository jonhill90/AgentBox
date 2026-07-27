# Reference — HTTP surface

A browser cannot speak Streamable HTTP MCP, so the viewer at `/ui` has its
own plain HTTP routes. Each is a thin wrapper over the very same internal
function the matching MCP tool calls — there is no second copy of the
browser logic.

## Routes

| Route | Body | Notes |
|---|---|---|
| `GET /health` | — | Open by design; the Docker healthcheck target |
| `GET /ui` | — | The viewer page; open so it can load and then ask for a token |
| `GET /api/auth-required` | — | `{auth_required, terminal_enabled}` — how the page learns whether to prompt |
| `GET /vendor/{name}` | — | The viewer's own vendored scripts (xterm.js) |
| `GET /api/screenshot` | — | `{screenshot: base64 PNG, url, bytes}`; **404** `Browser not active` until a page exists |
| `POST /api/browser/navigate` | `{url}` | |
| `POST /api/browser/click` | `{x_percent, y_percent}` | |
| `POST /api/browser/scroll` | `{delta_x, delta_y}` | Both default to 0 |
| `POST /api/browser/keypress` | `{key}` | |
| `POST /api/browser/type` | `{text}` | |
| `POST /api/browser/history` | `{action}` | `back` \| `forward` \| `reload` |
| `POST /api/browser/element` | `{x_percent, y_percent}` | Describe mode: element info, no click |

Plus `POST /mcp` — the Streamable HTTP MCP endpoint itself — and the
terminal WebSocket, which is registered **only** when
`AGENTBOX_ENABLE_TERMINAL=true`. When it is off the route 404s; it does not
exist and refuse.

## Auth

When a token is configured, every `/api/*` route and `/mcp` require
`Authorization: Bearer <token>` and return **401** without it. The four
routes marked open above stay reachable on purpose — the page must load
before it can ask for a credential.

When no token is configured the guard is not wired in at all, and
everything that can reach the port can drive the browser. That is the
default, and it is acceptable only because the port binds to `127.0.0.1`.
Enable auth before exposing it anywhere: see
[`../runbooks/enable-auth.md`](../runbooks/enable-auth.md).

Note the distinction when verifying: **401 means auth is working; 404 means
you used a path that does not exist** and proves nothing either way.

## Error shape

Browser-level failures return HTTP 200 with
`{"success": false, "error": ...}`, matching the MCP surface. Only
malformed requests get a 400.

## DNS-rebinding guard

Both `Origin` and `Host` are validated by ASGI middleware before any route
runs — a page on an attacker's origin cannot drive this server even though
it binds to loopback. Configured with `AGENTBOX_ALLOWED_HOSTS` and
`AGENTBOX_ALLOWED_ORIGINS`. Detail:
[`../architecture/security.md`](../architecture/security.md)
§ DNS-rebinding guard.
