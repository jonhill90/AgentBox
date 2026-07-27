# 0004 — Secrets come from files, never from the environment

**Status:** accepted.

## Decision

Every secret has a `*_FILE` form that holds a **path**:
`AGENTBOX_AUTH_TOKEN_FILE`, `AGENTBOX_GIT_CREDENTIALS_FILE`,
`AGENTBOX_GIT_SSH_KEY_FILE`. Pair them with a Compose `secrets:` block.
Reading an unreadable `*_FILE` is a `SystemExit` — fail closed, never fall
back to running without the secret.

## Why

`docker inspect` returns environment values verbatim to anyone with Docker
socket access. The audit of this repo demonstrated exactly that leak for
the auth token. With the file form the environment holds only a path, and
the value lives in a mount the operator controls.

This is the Docker Official Images `*_FILE` convention, not an invention.

## Consequences

- Direct value forms (`AGENTBOX_AUTH_TOKEN=…`) still exist for quick local
  work, and are documented as the weaker option.
- The token is scrubbed from the process environment after loading.
- Startup logs a SHA-256 fingerprint, never the secret.
- Git credentials are additionally **read-only**: the container gets a
  helper that answers `get` and ignores `store`/`erase`, so nothing inside
  can rewrite the operator's secret through git.

**Detail:** [`../architecture/security.md`](../architecture/security.md)
§§ Auth, Git push credentials.
