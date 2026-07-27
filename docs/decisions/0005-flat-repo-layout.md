# 0005 — Flat layout, and the specs live in `docs/`

**Status:** accepted, 2026-07-26. Amended the same day — see *Amendment*.

## Decision

One service, one repository, flat:

- `src/`, `tests/`, `theme/` at the root
- `scripts/` for the entrypoint and the git credential helper
- `docs/` for **all** documentation *and* the specs (`PRD.md`, `SPEC.md`),
  split into `architecture/ decisions/ development/ reference/ runbooks/`
- `docker-compose.yml` at the root
- `AGENTS.md` and `CLAUDE.md` are the same file, one a symlink

## Rejected: mirroring hill90-app's `services/<name>/`

hill90-app nests each service because it hosts several. AgentBox is one
service in its own repository, so `services/agentbox/` would be a directory
that only ever holds one thing, at the cost of rewriting every `COPY` path,
the Compose build context, test imports, and every doc cross-reference.

## `docker-compose.yml` stays at the root

hill90-app keeps compose files under `compose/` because it has several
profiles. Here there is one, and the root filename is what `docker compose
up` finds with no `-f` argument — moving it would add a flag to every
command in every doc, test and runbook, for no gain.

## Amendment — the specs moved back into `docs/`

This ADR originally put `PRD.md` and `SPEC.md` at the repository root,
because that is where hill90-app keeps them. The operator's call is that
they belong in `docs/`, and that is now the layout.

The reasoning that holds it: the root should carry only what a reader needs
*before* they have decided to go deeper — `README.md` and `AGENTS.md`.
Anything that answers a specific question, specs included, sits one level
down where the index can route to it. A root holding four Markdown files of
unclear precedence is precisely what progressive disclosure is meant to
avoid.

Consequence: every path reference is `docs/SPEC.md`, not `SPEC.md`. Section
citations in prose (`SPEC §14`) are unchanged.

## Consequences

- Merging this repo into hill90-app later would require the `services/`
  move avoided here. Judged cheaper once, later, than carried now.
- Two conventions now differ from hill90-app deliberately: no `services/`
  nesting, and specs under `docs/`. Anyone reconciling the two repos should
  read this record rather than assume drift.
