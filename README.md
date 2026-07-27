# AgentBox

An MCP server that gives an agent a real, **persistent** Chromium browser it
can drive over Streamable HTTP — navigate, screenshot, click, read text, run
JS, and click/type/scroll by coordinate, all against the same live page
across separate tool calls, and across separate clients. It also ships a
**take-control viewer** at `/ui` so you can watch that page live and grab the
wheel yourself, plus — each behind its own toggle — workspace-scoped
filesystem, git and SSRF-protected HTTP tools, and an interactive terminal.

**Status: Phase 1** — local Docker only, on the operator's machine. No cloud
deployment, no Tailscale.

## Quick start

```bash
git clone https://github.com/jonhill90/AgentBox.git && cd AgentBox
cp .env.example .env          # optional — the defaults are sensible
docker compose up -d --build

curl http://localhost:8054/health
# {"status":"healthy","service":"agentbox","version":"1.0.0"}
```

**MCP endpoint** `http://localhost:8054/mcp` (transport `streamable-http`) ·
**Viewer** <http://localhost:8054/ui>

Add it to an MCP client:

```bash
claude mcp add --transport http agentbox http://localhost:8054/mcp
# with auth enabled, add:  --header "Authorization: Bearer $TOKEN"
```

## Where to go next

This file is the entry point and stays short. Everything else is one link away.

| You want | Read |
|---|---|
| **To work on this repo as an agent** | **`AGENTS.md`** (`CLAUDE.md` is the same file) |
| Every tool, its arguments and its containment | [`docs/reference/tools.md`](docs/reference/tools.md) |
| The HTTP routes and their auth behaviour | [`docs/reference/http-api.md`](docs/reference/http-api.md) |
| How the pieces fit together | [`docs/architecture/overview.md`](docs/architecture/overview.md) |
| The threat model and every security decision | [`docs/architecture/security.md`](docs/architecture/security.md) |
| Running, testing, debugging, and the traps | [`docs/development/local-setup.md`](docs/development/local-setup.md) |
| Why a design is the way it is | [`docs/decisions/`](docs/decisions/) |
| Enabling auth, the terminal, or git push | [`docs/runbooks/`](docs/runbooks/) |
| What is required, and the scope boundary | [`docs/PRD.md`](docs/PRD.md) · [`docs/SPEC.md`](docs/SPEC.md) |

The full map with one-line summaries is **[`docs/README.md`](docs/README.md)**.

## How the persistence works

A dedicated daemon thread runs a forever asyncio loop that owns one
Playwright `Page` for the life of the process. Every tool dispatches into
that loop via `asyncio.run_coroutine_threadsafe`, so the page is never
recreated per call — its DOM, JS globals, cookies and session history all
survive between calls, including across separate MCP client connections.

The browser is built lazily on the first tool call that needs it and lives
until the process exits. `docker compose restart` therefore gives you a
clean browser; there is no tool that resets it.

Detail: [`docs/architecture/overview.md`](docs/architecture/overview.md).

## Configuration

Everything lives in `.env`, read by `docker-compose.yml`. `.env.example`
documents every variable.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTBOX_PORT` | `8054` | Host port mapped to the container's `8000` |
| `AGENTBOX_BIND` | `127.0.0.1` | Host interface to publish on |
| `LOG_LEVEL` | `info` | Server log level |
| `AGENTBOX_ENABLE_JUMPBOX_TOOLS` | `true` | Register the filesystem/git/http tools |
| `AGENTBOX_ENABLE_TERMINAL` | `false` | Register the PTY WebSocket; also requires a token |
| `AGENTBOX_AUTH_TOKEN` | *(empty)* | Shared secret. Empty = no auth |
| `AGENTBOX_AUTH_TOKEN_FILE` | *(empty)* | Read the secret from a file instead — **preferred** |
| `AGENTBOX_AUTH_TOKEN_PREVIOUS` | *(empty)* | Outgoing token, accepted during a rotation |
| `AGENTBOX_GIT_CREDENTIALS_FILE` | *(empty)* | HTTPS push credentials; unset = clone/pull only |
| `AGENTBOX_GIT_SSH_KEY_FILE` | *(empty)* | SSH push key |
| `AGENTBOX_GIT_KNOWN_HOSTS_FILE` | *(empty)* | Host keys; `StrictHostKeyChecking` stays on |
| `AGENTBOX_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],0.0.0.0` | DNS-rebinding allowlist |
| `AGENTBOX_ALLOWED_ORIGINS` | *(same-host)* | Extra browser origins to accept |

Prefer the `*_FILE` form for every secret — an environment value is returned
verbatim by `docker inspect`. See
[`docs/decisions/0004-secrets-from-files.md`](docs/decisions/0004-secrets-from-files.md).

The compose file defines one service, no external network, no Docker socket,
and two named volumes: `agentbox-workspace` → `/workspace` and
`agentbox-screenshots` → `/workspace/screenshots`. Files written by
`write_file` and commits made by the `git` tool survive
`docker compose down`; `docker volume rm agentbox_agentbox-workspace` starts
clean. Limits are 1 CPU, 1 GB memory, 200 PIDs.

## Everyday commands

```bash
docker compose logs -f                       # logs
docker compose restart                       # restart — also gives a fresh browser
docker compose exec agentbox bash            # shell inside the container
docker compose exec agentbox ps -ef          # processes (procps is installed)
.venv-test/bin/python -m pytest tests/ -q    # full suite, ~5 min, needs the container
```

More, including the traps that a green suite does not catch:
[`docs/development/local-setup.md`](docs/development/local-setup.md).

## Scope

`docs/SPEC.md` is the source of truth. §14 is Phase 2 and is **not built**:
no OAuth, no cloud deploy, no Tailscale. AKM/knowledge tools are
**permanently** out of scope — not deferred.
