#!/usr/bin/env python3
"""
Git push credentials (SPEC §16) — file-backed, opt-in, never in the environment.

WHY THIS IS SHAPED THIS WAY

Pushing means this box can write to your source of truth. PRD 1.5 already
names the threat that makes that serious: the browser reads untrusted web
content and the terminal grants a shell, which is the prompt-injection-to-RCE
path. Adding push credentials moves the blast radius from "this container" to
"your repositories". So the design is deliberately narrow:

1. OFF BY DEFAULT. No credential configured means git works exactly as before
   — clone and pull over HTTPS, no push. Nothing is implicit.

2. FILE, NOT ENVIRONMENT. An environment variable is returned verbatim by
   `docker inspect` to anyone with Docker socket access; the audit of this
   repo demonstrated exactly that leak for the auth token. Credentials are
   read from a path, so the environment holds only the path. Pair with a
   Compose `secrets:` block.

3. NEVER WRITTEN BY US. Git's `store` helper would happily *write* new
   credentials into its file. It is configured read-only here — we hand git a
   helper that only ever answers `get`, so a `store` or `erase` from anywhere
   in the container cannot modify or delete the operator's secret.

4. SCOPE IT AT THE SOURCE. Nothing here can limit what a credential can do —
   that is the credential's job. Use a fine-grained PAT restricted to the
   specific repositories that need writing, or a per-repository deploy key.
   A broad personal token hands an injected agent your whole account, and no
   amount of care on this side changes that.

WHAT THIS CANNOT PROTECT AGAINST, stated plainly: when the terminal is
enabled the shell runs as the same uid as this process, so it can read the
credential file directly. That is inherent — git needs the secret, so any
process that can run git can obtain it. The mitigations are the scope of the
credential and the fact that the terminal is itself off by default.
"""

import logging
import os
import pathlib
import stat
import subprocess

logger = logging.getLogger(__name__)

# A file in git's credential-store format, one URL per line:
#   https://<user>:<token>@github.com
CREDENTIALS_FILE = os.environ.get("AGENTBOX_GIT_CREDENTIALS_FILE", "").strip()

# A private key for git over SSH.
SSH_KEY_FILE = os.environ.get("AGENTBOX_GIT_SSH_KEY_FILE", "").strip()

# Host keys for the forge. Without this, SSH either refuses to connect or —
# far worse — is told not to check, which trades a push credential for a
# machine-in-the-middle.
SSH_KNOWN_HOSTS = os.environ.get("AGENTBOX_GIT_KNOWN_HOSTS_FILE", "").strip()


def push_enabled() -> bool:
    return bool(CREDENTIALS_FILE or SSH_KEY_FILE)


def _warn_if_world_readable(path: str) -> None:
    try:
        mode = pathlib.Path(path).stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "Git credential file %s is group/world readable (mode %s). "
            "Mount it 0400.", path, oct(stat.S_IMODE(mode)),
        )


def configure() -> None:
    """Wire the credentials into git config. Safe to call when none are set."""
    if not push_enabled():
        logger.info("Git push DISABLED (no AGENTBOX_GIT_CREDENTIALS_FILE / _SSH_KEY_FILE)")
        return

    def git_config(key: str, value: str) -> None:
        subprocess.run(["git", "config", "--global", key, value],
                       capture_output=True, check=False)

    if CREDENTIALS_FILE:
        if not pathlib.Path(CREDENTIALS_FILE).is_file():
            # Loud, not silent: a configured-but-missing credential should not
            # look the same as "push was never set up".
            logger.error("AGENTBOX_GIT_CREDENTIALS_FILE=%s does not exist; push will fail",
                         CREDENTIALS_FILE)
        else:
            _warn_if_world_readable(CREDENTIALS_FILE)
            # A read-only shim rather than `store --file=`: the built-in
            # helper also implements store/erase, so anything in the container
            # could rewrite the operator's secret through git.
            git_config("credential.helper", f"!/usr/local/bin/agentbox-git-credential")
            logger.info("Git push ENABLED via credential file %s", CREDENTIALS_FILE)

    if SSH_KEY_FILE:
        if not pathlib.Path(SSH_KEY_FILE).is_file():
            logger.error("AGENTBOX_GIT_SSH_KEY_FILE=%s does not exist; ssh push will fail",
                         SSH_KEY_FILE)
        else:
            _warn_if_world_readable(SSH_KEY_FILE)
            known = SSH_KNOWN_HOSTS or "/home/agentbox/.ssh/known_hosts"
            # StrictHostKeyChecking stays ON. Turning it off is the usual
            # shortcut here and it converts a push credential into a
            # machine-in-the-middle opportunity.
            git_config("core.sshCommand", (
                f"ssh -i {SSH_KEY_FILE} -o IdentitiesOnly=yes "
                f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={known}"
            ))
            if not pathlib.Path(known).is_file():
                logger.warning(
                    "No known_hosts at %s — ssh pushes will refuse to connect until "
                    "host keys are provided (this is the safe failure, not a bug).", known,
                )
            logger.info("Git push over SSH ENABLED with key %s", SSH_KEY_FILE)
