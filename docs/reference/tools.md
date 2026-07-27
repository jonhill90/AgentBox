# Reference — MCP tools

Every tool returns a JSON string containing a `success` boolean. Failures
come back as `{"success": false, "error": "..."}` (error text capped at 300
chars) rather than raising — a bad selector, an unreachable host, or JS that
throws leaves the browser alive and usable.

## Core browser tools (always registered)

The eleven tools from `docs/SPEC.md` §5. These ship unconditionally.

| Tool | Arguments | Returns |
|---|---|---|
| `navigate` | `url` | `url`, `status`, `title` |
| `screenshot` | `full_page=True` | `image_base64` (PNG), `path`, `bytes`, `url` |
| `click` | `selector` | `url`, `title` |
| `get_text` | `selector="body"` | `text` (truncated at 4000 chars) |
| `evaluate` | `script` | `result` (JSON-encoded, truncated at 4000 chars) |
| `click_at_percent` | `x_percent`, `y_percent` | resolved `x`/`y` pixels, `url` |
| `type_at` | `text`, optional `x_percent`/`y_percent` | `url` |
| `press_key` | `key` (`Enter`, `Tab`, `Escape`, …) | `url` |
| `scroll` | `delta_x=0`, `delta_y=0` | `url` |
| `history` | `action`: `back` \| `forward` \| `reload` | `url`, `title` |
| `health` | — | `status`, `service`, `version`, `browser_started` |

`screenshot` also writes a PNG to `/workspace/screenshots` inside the
container.

`type_at` given `x_percent`/`y_percent` clicks that point to focus it first;
without them it types into whatever is already focused.

## Jumpbox tools (behind `AGENTBOX_ENABLE_JUMPBOX_TOOLS`)

These five are registered **only when the toggle is on**. When it is off
they do not appear in `list_tools()` at all — see
[`../decisions/0002-toggles-unregister-tools.md`](../decisions/0002-toggles-unregister-tools.md).

| Tool | Arguments | Notes |
|---|---|---|
| `read_file` | `path` | First 1 MB; must be inside `/workspace` |
| `write_file` | `path`, `content` | Creates parent directories; `/workspace` only |
| `list_directory` | `path` | `name`, `type`, `size` per entry |
| `git` | `action`, `paths`, `message`, `count` | Fixed set: `init`, `status`, `add`, `commit`, `diff`, `log`, `reset` |
| `http_request` | `url`, `method`, `headers`, `body` | GET/POST only; SSRF-blocked |

Containment, which is the interesting part:

- **`PathPolicy` scopes the filesystem tools to `/workspace`.** Paths are
  `realpath`-resolved before checking, so `..` traversal and symlinks are
  judged on their target, and `/workspaceless` is not treated as a child of
  `/workspace`. Default-deny: a path must match an allowed root.
- **`git` is a fixed subcommand set, not a passthrough.** There is no
  `git <arbitrary args>`; unknown actions are refused. Adding a subcommand
  is a reviewed code change.
- **`http_request` resolves the hostname and checks the resolved IP**
  against loopback, RFC1918, link-local (including cloud metadata at
  169.254.169.254), and the 100.64.0.0/10 CGNAT range Tailscale uses — so a
  DNS name pointing at an internal address does not get through.
- **Redirects are followed one hop at a time and every hop is re-checked.**
  A public URL that 302s to `http://169.254.169.254/` is refused at the
  redirect, with the same `{"success": false, "error": ...}` shape as a
  direct block. Chains are capped at 3 hops.

Full reasoning: [`../architecture/security.md`](../architecture/security.md)
§§ Filesystem scoping, SSRF.

## What is deliberately absent

There is **no shell or exec tool** on the MCP surface. Interactive shell
access exists only as the PTY WebSocket, which is off by default and
requires auth — see
[`../decisions/0003-terminal-off-by-default.md`](../decisions/0003-terminal-off-by-default.md).

AKM/knowledge tools are **permanently** out of scope — not deferred.
