#!/usr/bin/env python3
"""
Git push credentials (SPEC.md §16).

The security-relevant claims, each with a test:

  - off by default — no credential means no push, and nothing implicit
  - the secret is read from a FILE, so it never appears in `docker inspect`
  - the helper is read-only — `store`/`erase` cannot rewrite the operator's
    secret, which git's own `store` helper would happily do
  - the helper answers only for the matching host
  - SSH keeps StrictHostKeyChecking on
"""

import subprocess
import time
import urllib.error
import urllib.request

import pytest

from conftest import CONTAINER, requires_docker_introspection

TOKEN = "ghp-fake-token-for-tests"
CREDS = f"https://agentbot:{TOKEN}@github.com\n"


def _image() -> str:
    return subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def creds_server(tmp_path_factory):
    """A container with a credential file mounted, as an operator would."""
    secret_dir = tmp_path_factory.mktemp("gitcreds")
    secret = secret_dir / "git_credentials"
    secret.write_text(CREDS)
    secret.chmod(0o444)          # readable by the app user inside
    secret_dir.chmod(0o755)

    name, port = "agentbox-gitcreds-test", 8065
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-v", f"{secret}:/run/secrets/git_credentials:ro",
        "-e", "AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/git_credentials",
        "-p", f"127.0.0.1:{port}:8000", _image(),
    ], capture_output=True, text=True, check=True)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _in(container: str, script: str, user: str = "agentbox") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-u", user, container, "sh", "-c", script],
        capture_output=True, text=True,
    )


# ── off by default ───────────────────────────────────────────────────

@requires_docker_introspection
def test_push_is_disabled_by_default():
    """The running container has no credential configured."""
    logs = subprocess.run(["docker", "logs", CONTAINER],
                          capture_output=True, text=True, check=True)
    assert "Git push DISABLED" in logs.stdout + logs.stderr


@requires_docker_introspection
def test_no_credential_helper_is_configured_by_default():
    out = _in(CONTAINER, "git config --global --get credential.helper || echo NONE")
    assert "NONE" in out.stdout, out.stdout


@requires_docker_introspection
def test_clone_over_https_still_works_without_credentials():
    """Read access must not require any of this."""
    out = _in(CONTAINER, "cd /tmp && rm -rf hw && "
                         "timeout 60 git clone --depth=1 -q "
                         "https://github.com/octocat/Hello-World.git hw && echo CLONED")
    assert "CLONED" in out.stdout, out.stdout + out.stderr


# ── with a credential file ───────────────────────────────────────────

@requires_docker_introspection
def test_credential_helper_is_wired_when_a_file_is_given(creds_server):
    out = _in(creds_server, "git config --global --get credential.helper")
    assert "agentbox-git-credential" in out.stdout, out.stdout


@requires_docker_introspection
def test_the_helper_returns_the_credential_for_a_matching_host(creds_server):
    out = _in(creds_server,
              "printf 'protocol=https\\nhost=github.com\\n\\n' | "
              "AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/git_credentials "
              "/usr/local/bin/agentbox-git-credential get")
    assert "username=agentbot" in out.stdout, out.stdout
    assert f"password={TOKEN}" in out.stdout, out.stdout


@requires_docker_introspection
def test_the_helper_stays_silent_for_other_hosts(creds_server):
    """A credential for github.com must not be handed to gitlab.com."""
    out = _in(creds_server,
              "printf 'protocol=https\\nhost=gitlab.com\\n\\n' | "
              "AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/git_credentials "
              "/usr/local/bin/agentbox-git-credential get")
    assert TOKEN not in out.stdout, out.stdout
    assert out.stdout.strip() == "", out.stdout


@requires_docker_introspection
def test_the_helper_is_read_only(creds_server):
    """git's built-in `store` helper implements store/erase, so anything in
    the container could rewrite the operator's secret through git. This one
    must ignore both and leave the file untouched."""
    before = _in(creds_server, "cat /run/secrets/git_credentials").stdout

    _in(creds_server,
        "printf 'protocol=https\\nhost=evil.com\\nusername=x\\npassword=y\\n\\n' | "
        "AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/git_credentials "
        "/usr/local/bin/agentbox-git-credential store")
    _in(creds_server,
        "printf 'protocol=https\\nhost=github.com\\n\\n' | "
        "AGENTBOX_GIT_CREDENTIALS_FILE=/run/secrets/git_credentials "
        "/usr/local/bin/agentbox-git-credential erase")

    after = _in(creds_server, "cat /run/secrets/git_credentials").stdout
    assert after == before, "the helper modified the credential file"
    assert "evil.com" not in after, after


@requires_docker_introspection
def test_the_secret_is_not_in_docker_inspect(creds_server):
    """The whole point of the file form."""
    env = subprocess.run(
        ["docker", "inspect", creds_server, "--format", "{{json .Config.Env}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert TOKEN not in env, "the credential leaked into docker inspect"
    assert "/run/secrets/git_credentials" in env, env


@requires_docker_introspection
def test_the_secret_is_not_logged(creds_server):
    logs = subprocess.run(["docker", "logs", creds_server],
                          capture_output=True, text=True, check=True)
    combined = logs.stdout + logs.stderr
    assert TOKEN not in combined, "the credential was logged"
    assert "Git push ENABLED" in combined, combined[-500:]


@requires_docker_introspection
def test_ssh_keeps_strict_host_key_checking(creds_server):
    """Disabling it is the usual shortcut, and it trades a push credential
    for a machine-in-the-middle. Assert we never emit that."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "git_credentials.py"
    text = src.read_text()
    assert "StrictHostKeyChecking=yes" in text
    assert "StrictHostKeyChecking=no" not in text
    assert "UserKnownHostsFile=/dev/null" not in text
