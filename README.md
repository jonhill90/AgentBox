# AgentBox

An MCP server that gives an agent a real, **persistent** Chromium browser it can
drive over Streamable HTTP — navigate, screenshot, click, read text, run JS, and
click/type/scroll by coordinate, all against the same live page across separate
tool calls.

**Status: Phase 1** — local Docker only, on the operator's machine. No auth, no
cloud deployment, no Tailscale. See `docs/PRD.md` and `docs/SPEC.md`.

## How the persistence works

A dedicated daemon thread runs a forever asyncio loop that owns one Playwright
`Page` for the life of the process. Every tool dispatches into that loop via
`asyncio.run_coroutine_threadsafe`, so the page is never recreated per call and
its DOM, JS globals, cookies, and session history all survive between calls.
This pattern is ported from Hill90's `services/agentbox/app/tools.py`.

## Quick Start

```bash
cp .env.example .env      # optional; defaults are fine

docker compose up -d --build

curl http://localhost:8054/health
# {"status":"healthy","service":"agentbox","version":"1.0.0"}
```

**MCP endpoint**: `http://localhost:8054/mcp` — transport `streamable-http`.

## MCP Tools (11 total)

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
| `health` | — | `status`, `browser_started` |

Every tool returns a JSON string with a `success` boolean; failures come back as
`{"success": false, "error": "..."}` rather than raising.

Screenshots are also written to `/workspace/screenshots` inside the container
(the `agentbox-screenshots` named volume).

There is **no shell/exec tool**. `execute_command` and `manage_process` were
removed deliberately — a browser-only box has a far smaller blast radius. See
PRD §1.2 before considering adding them back.

## Testing

The integration test proves the persistent-loop pattern end to end: it navigates,
takes a screenshot and asserts a real non-empty PNG comes back, then shows the
page is the *same* page two independent ways — a JS global set in one `evaluate`
call is readable by a later separate call, and `history back` after a second
`navigate` returns to the first URL (a fresh page would have no history).

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install pytest pytest-asyncio 'mcp>=1.2.0'
.venv-test/bin/python -m pytest tests/test_integration.py -v
```

If nothing is listening on `http://localhost:8054/health`, the test runs
`docker compose up -d --build` itself and tears it down afterwards. If the
container is already up, it uses it and leaves it running. Set `AGENTBOX_URL` to
point at a different host/port.

## Configuration

`.env` options:
- `AGENTBOX_PORT` — external port (default: 8054)
- `LOG_LEVEL` — logging level (info, debug, warning, error)

## Architecture

```
python:3.12-slim-bookworm
├── Chromium system libs (apt — see Dockerfile)
├── Playwright + Chromium browser (/data/browsers)
├── Python deps (fastmcp, pydantic, uvicorn, playwright)
└── src/mcp_server.py
    ├── persistent browser loop (daemon thread, owns the Page)
    └── FastMCP streamable-http on :8000 → :8054
```

Debian is required: Playwright/Chromium does not run on musl libc, so the
original Alpine base could not work. Resource limits: 1 CPU, 1 GB RAM, 200 PIDs.
No Docker socket is mounted.

## Troubleshooting

```bash
docker compose logs -f                 # logs
curl http://localhost:8054/health      # health
docker compose exec agentbox bash      # shell
docker compose restart                 # restart (also resets the browser page)
```

## Files

```
agentbox/
├── Dockerfile              # Debian slim + Playwright/Chromium
├── docker-compose.yml      # single service, local only
├── pyproject.toml          # Python dependencies
├── docs/                   # PRD.md, SPEC.md
├── src/mcp_server.py       # FastMCP server + persistent browser loop
└── tests/test_integration.py
```
