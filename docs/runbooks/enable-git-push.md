# Runbook — give AgentBox write access to a repository

Off by default: with no credential configured, git clones and pulls over
HTTPS and cannot push. Nothing here is implicit.

**Scope the credential at the source.** Nothing in this container can limit
what a credential can do — that is the credential's job. Use a
fine-grained PAT restricted to specific repositories, or a per-repository
deploy key. A broad account token hands an injected agent everything.

## Option A — HTTPS with a fine-grained PAT

### 1. Create the token

github.com → Settings → Developer settings → Personal access tokens →
Fine-grained tokens. Select **only** the repositories that need writing,
and grant **Contents: Read and write**. Set the shortest expiry you can
live with.

### 2. Write it in git's credential-store format

```bash
mkdir -p secrets && chmod 700 secrets
printf 'https://<username>:<token>@github.com\n' > secrets/git_credentials
chmod 400 secrets/git_credentials
```

One URL per line, no trailing spaces. `secrets/` is gitignored.

### 3. Point the container at the file

```
AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/agentbox_git_credentials
```

Mount it as a Compose secret and `docker compose up -d`.

The container configures `credential.helper` to a **read-only** shim that
answers `get` and ignores `store`/`erase`, so nothing inside can rewrite
your secret through git.

### 4. Verify — including the negative

Push to an in-scope repository: it should succeed. Then try one that is
**not** in the token's repository list: it must fail with 403. If an
out-of-scope push succeeds, the token is broader than you think — revoke
it and start again.

## Option B — SSH with a deploy key

### 1. Generate a key and add the public half as a deploy key

```bash
ssh-keygen -t ed25519 -f secrets/git_ssh_key -N '' -C agentbox
chmod 400 secrets/git_ssh_key
```

Add `secrets/git_ssh_key.pub` to the repository's Deploy keys with **Allow
write access** checked. Per-repository by construction.

### 2. Provide host keys — do not skip this

```bash
ssh-keyscan github.com > secrets/known_hosts
```

`StrictHostKeyChecking` stays **on**. Turning it off is the usual shortcut
and it converts a push credential into a machine-in-the-middle
opportunity. With no `known_hosts` the connection is refused — that is the
safe failure, not a bug.

### 3. Configure

```
AGENTBOX_GIT_SSH_KEY_FILE=/run/secrets/agentbox_git_ssh_key
AGENTBOX_GIT_KNOWN_HOSTS_FILE=/run/secrets/agentbox_known_hosts
```

### 4. Verify — including both negatives

- Push with the key configured: succeeds.
- Unset the key: push must fail.
- Point `known_hosts` at a wrong host key: the connection must be refused,
  not silently accepted.

## Revoking

Remove the file and the environment variable, then `docker compose up -d`.
Also revoke at the forge — a deleted local file is not a revoked
credential.

**Detail:** [`../architecture/security.md`](../architecture/security.md)
§ Git push credentials, and `SPEC.md` §16.
