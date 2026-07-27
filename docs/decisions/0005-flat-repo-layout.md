# 0005 — `src/` at the repo root, not `services/agentbox/`

**Status:** accepted, 2026-07-26.

## Decision

Adopt hill90-app's *conventions* — `PRD.md`/`SPEC.md` at the repo root,
`docs/` split into `architecture/ decisions/ development/ runbooks/`,
shell scripts under `scripts/` — but keep the application flat: `src/`,
`tests/`, `theme/` at the root.

## Rejected: mirroring `services/<name>/`

hill90-app nests each service because it hosts several. AgentBox is one
service in its own repository, so `services/agentbox/` would be a
directory that only ever holds one thing, at the cost of rewriting every
`COPY` path, the Compose build context, test imports, and every doc
cross-reference.

## `docker-compose.yml` stays at the root

hill90-app keeps compose files under `compose/` because it has several
profiles. Here there is one, and the root filename is what `docker compose
up` finds with no `-f` argument — moving it would add a flag to every
command in every doc, test and runbook, for no gain.

## Consequences

- Merging this repo into hill90-app later would require the move that was
  avoided here. Judged cheaper once, later, than carried now.
- `PRD.md` and `SPEC.md` moved out of `docs/` — they are inputs to the
  work, not documentation of it, which is also where hill90-app puts them.
