# Runbook — rotate the auth token without downtime

`AGENTBOX_AUTH_TOKEN_PREVIOUS` exists so rotation does not need coordinated
downtime. Without an overlap window rotation does not happen in practice,
which is the actual security problem.

Both tokens are accepted while both are set.

## 1. Move the current token to the previous slot

```bash
cp secrets/auth_token secrets/auth_token_previous
chmod 400 secrets/auth_token_previous
```

## 2. Generate the new one

```bash
openssl rand -hex 32 > secrets/auth_token
chmod 400 secrets/auth_token
```

## 3. Restart with both set

Set `AGENTBOX_AUTH_TOKEN_PREVIOUS` (or its file form) alongside the current
token, then:

```bash
docker compose up -d
```

Both tokens now work. Verify the old one still does before you rely on it:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(cat secrets/auth_token_previous)" \
  http://127.0.0.1:8054/api/screenshot    # 200
```

## 4. Move every client to the new token

Take as long as you need — this is the point of the window.

## 5. Close the window

Clear `AGENTBOX_AUTH_TOKEN_PREVIOUS`, remove
`secrets/auth_token_previous`, restart, and confirm the old token is dead:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer <old token>" \
  http://127.0.0.1:8054/api/screenshot    # 401
```

**Do not skip step 5.** An overlap left open forever is two live
credentials, one of which nobody is tracking.
