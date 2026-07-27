# AgentBox — agent orientation

*(`AGENTS.md` and `CLAUDE.md` are the same file — one is a symlink, so there is no second copy to drift.)*

Read this first. It is deliberately short; follow the links only when you
need them.

**What this is:** a self-hosted MCP server in Docker that gives an agent a
real, persistent Chromium browser, plus workspace-scoped filesystem/git/HTTP
tools and an interactive PTY. There is a browser UI at `/ui` for watching and
taking over. Local-only, single operator, Phase 1.

## Where to look

| You need | Read |
|---|---|
| What is required and why | `docs/PRD.md` (product) then `docs/SPEC.md` (engineering) |
| How the pieces fit together | `docs/architecture/overview.md` |
| The threat model and every security decision | `docs/architecture/security.md` |
| Running, testing, debugging | `docs/development/local-setup.md` |
| Why a design is the way it is | `docs/decisions/` (short ADRs) |
| How to enable auth / terminal / git push | `docs/runbooks/` |
| Operator-facing usage | `README.md` |
| The full documentation map | `docs/README.md` |

`docs/SPEC.md` is the source of truth for scope. Sections map to
implementation; §14 is Phase 2 and is **not to be built**.

## Layout

Flat by design — one service, one repo.

- `docs/` — the specs (`PRD.md`, `SPEC.md`) and all documentation
- `src/` — the application, six modules
- `tests/` — the suite; `theme/` — tmux/zsh/p10k
- `scripts/` — entrypoint and the git credential helper
- `secrets/` — gitignored, never commit anything under it

Why this is *not* `services/agentbox/`, and why the specs sit in `docs/`:
`docs/decisions/0005-flat-repo-layout.md`.

## Invariants — do not break these without an explicit decision

These are not style preferences. Each was a deliberate call with a reason
recorded in `docs/architecture/security.md`; several were bought with real bugs.

1. **The browser page is never recreated per call.** One Playwright `Page`
   lives on a dedicated asyncio loop in a background thread for the life of
   the process. Everything dispatches into it. This is the whole point of
   the project — see `docs/architecture/overview.md`.
2. **Toggled-off means absent, not refusing.** When a feature flag is off,
   its tools/routes are never registered — they do not appear in
   `list_tools()` and the route 404s. A tool that exists and returns
   "disabled" is still attack surface.
3. **No credential ever goes in a URL.** RFC 9700 §4.3.2 is a MUST NOT. The
   terminal WebSocket authenticates by header or by first message, and a
   `?token=` query parameter is *refused*, not honoured.
4. **The PTY spawns nothing before authentication.** Binary frames before
   auth close the socket. This is the ttyd pre-auth RCE class.
5. **The container does not run as root**, and the shell does not inherit
   the server's environment (it would leak the auth token).
6. **Filesystem tools stay inside `/workspace`**, enforced by realpath
   resolution in `PathPolicy`, default-deny.
7. **AKM/knowledge tools are permanently out of scope.** Not deferred.
8. **Secrets are read from files, never environment variables**, and never
   logged — an env value is visible in `docker inspect`.

## Ground rules for changing this repo

- **Spec first.** The operator writes `docs/PRD.md` / `docs/SPEC.md`
  sections, then work is built against them. Do not invent scope. If a spec
  section is marked DRAFT or carries a `DECISION` marker, stop and ask.
- **Verify, do not assert.** Run the suite from a clean rebuild and paste
  real output. A claim without command output does not count.
- **Look at the real thing.** Several bugs here were invisible to a green
  test suite and only appeared when the actual page was opened in a browser
  — a resize frame sent before authentication, a CSS rule defeating
  `[hidden]`, a font subset that rendered blank. If you change the UI,
  open it.
- **Beware vacuous tests.** A prior test sent a literal `\n` instead of a
  newline, so the command never ran and the assertion matched the shell
  prompt. When asserting on terminal output, pick a string that cannot
  appear unless the command actually executed.
- `hill90-app/` and `Hill90/` outside this repo are READ-ONLY reference.
  Never write to them. Never touch `DebateWho`.

## Fast facts

```bash
docker compose up -d --build          # start (127.0.0.1:8054 only)
.venv-test/bin/python -m pytest tests/ -q    # 257 pass / 3 skip, ~5 min, needs the container
```

- MCP endpoint `http://localhost:8054/mcp` (Streamable HTTP) · viewer `/ui`
- Config lives in `.env`; `.env.example` documents every variable
- Auth is **off** unless `AGENTBOX_AUTH_TOKEN` is set; the terminal is **off**
  unless `AGENTBOX_ENABLE_TERMINAL=true` *and* a token is set
- Source is `src/` — six modules; `mcp_server.py` (~1066), `jumpbox_tools.py`
  and `terminal.py` are the large ones
