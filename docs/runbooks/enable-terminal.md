# Runbook — enable the terminal

## What you are accepting

A shell, running as the same uid as the server, in a container whose
browser reads untrusted web content. That is the
prompt-injection-to-RCE path named in `PRD.md` §1.5. It is off by default
for this reason — see
[`decisions/0003-terminal-off-by-default.md`](../decisions/0003-terminal-off-by-default.md).

If git push credentials are also configured, the shell can read them
directly. Scope the credential accordingly.

## 1. Enable auth first — it is a hard prerequisite

Follow [`enable-auth.md`](enable-auth.md). With no token configured the
WebSocket refuses every connection with close code 4001. That is
fail-closed behaviour, not a bug to work around.

## 2. Turn it on

In `.env`:

```
AGENTBOX_ENABLE_TERMINAL=true
```

```bash
docker compose up -d
```

## 3. Verify

Open `http://127.0.0.1:8054/ui`, enter the token, and switch to the
**Terminal** tab. You should get a Powerlevel10k prompt in tmux with the
OS icon and, inside a git repository, branch and status icons.

A shell-computed check is the honest one — zsh echoes the command back, so
a literal marker proves nothing about execution:

```
echo agentbox-$((21*2))-works
```

Seeing `agentbox-42-works` means the shell ran it. Seeing only the command
text means it did not.

## 4. Turning it back off

Clear the flag and `docker compose up -d`. The route disappears; it does
not linger refusing connections.
