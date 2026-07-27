# AgentBox documentation

Progressive disclosure: start at the top, go deeper only when you need to.

| Start here | |
|---|---|
| [`../AGENTS.md`](../AGENTS.md) | Agent entry point — invariants, ground rules, traps (`CLAUDE.md` is the same file) |
| [`../README.md`](../README.md) | What AgentBox is, and how to start it |
| [`PRD.md`](PRD.md) | What is required, and why (product) |
| [`SPEC.md`](SPEC.md) | The engineering spec — **source of truth for scope** |

## reference/ — the surfaces

| | |
|---|---|
| [`reference/tools.md`](reference/tools.md) | Every MCP tool, its arguments, and how it is contained |
| [`reference/http-api.md`](reference/http-api.md) | HTTP routes, auth behaviour, error shape |

## architecture/ — how it fits together

| | |
|---|---|
| [`overview.md`](architecture/overview.md) | One process, three surfaces; the browser loop; modules; request path |
| [`security.md`](architecture/security.md) | Threat model, every security decision with citations, known limitations |

## decisions/ — why it is this way

Short records of the load-bearing calls. Each states the decision, the
alternative rejected, and where the detail lives.

| | |
|---|---|
| [`0001-bearer-token-not-oauth.md`](decisions/0001-bearer-token-not-oauth.md) | Shared bearer token for Phase 1 |
| [`0002-toggles-unregister-tools.md`](decisions/0002-toggles-unregister-tools.md) | Disabled tools are absent, not refusing |
| [`0003-terminal-off-by-default.md`](decisions/0003-terminal-off-by-default.md) | The one toggle that defaults off |
| [`0004-secrets-from-files.md`](decisions/0004-secrets-from-files.md) | The `*_FILE` convention, never the value |
| [`0005-flat-repo-layout.md`](decisions/0005-flat-repo-layout.md) | Why `src/` at root rather than `services/agentbox/` |

## development/ — working on it

| | |
|---|---|
| [`local-setup.md`](development/local-setup.md) | Run it, test it, debug it, and the traps |

## runbooks/ — operational procedures

| | |
|---|---|
| [`enable-auth.md`](runbooks/enable-auth.md) | Turn on the bearer token |
| [`rotate-auth-token.md`](runbooks/rotate-auth-token.md) | Rotate without downtime |
| [`enable-terminal.md`](runbooks/enable-terminal.md) | Turn on the shell (and what you accept by doing so) |
| [`enable-git-push.md`](runbooks/enable-git-push.md) | Give the box write access to a repository |
