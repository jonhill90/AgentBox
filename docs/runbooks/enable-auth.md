# Runbook — enable the bearer token

Auth is off by default. Everything below assumes the repo root as cwd.

## 1. Generate a token

```bash
mkdir -p secrets && chmod 700 secrets
openssl rand -hex 32 > secrets/auth_token
chmod 400 secrets/auth_token
```

`secrets/` is gitignored. Never commit anything under it.

## 2. Point the container at the file, not the value

In `.env` (also gitignored):

```
AGENTBOX_AUTH_TOKEN_FILE=/run/secrets/agentbox_auth_token
```

and mount it as a Compose secret. Use the file form, not
`AGENTBOX_AUTH_TOKEN=<value>` — see
[`decisions/0004-secrets-from-files.md`](../decisions/0004-secrets-from-files.md).

```bash
docker compose up -d --build
```

## 3. Verify

The startup log prints a SHA-256 fingerprint of the token, never the token:

```bash
docker compose logs agentbox | grep -i fingerprint
```

Unauthenticated calls must now fail, and `/health` must still pass:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8054/api/state   # 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8054/health      # 200
```

401 on the first and 200 on the second is the check. If `/api/state`
returns 200, the token did not load — look for the fingerprint line.

## 4. Point clients at it

MCP clients send `Authorization: Bearer <token>`. The viewer at `/ui`
discovers it needs one via `GET /api/auth-required` and prompts.

**Detail:** [`../architecture/security.md`](../architecture/security.md) § Auth.
