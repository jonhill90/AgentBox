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

The SSH half is proven against a real sshd (see `fixtures/git-ssh-server/`)
rather than asserted from the source: a clone, a push, and a fresh re-clone
that shows the pushed commit, plus the two failures that matter — no key
means no push, and a known_hosts that does not match the server means the
push REFUSES instead of trusting whatever answers.
"""

import pathlib
import shutil
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


# ── git over SSH, against a real sshd ────────────────────────────────
#
# SPEC §16 says the SSH path keeps StrictHostKeyChecking=yes and needs a
# known_hosts file. Asserting that from the source only proves what we
# wrote; these run a real server so the property is observed, not claimed.

SSH_NET = "agentbox-sshtest-net"
SSH_SERVER = "agentbox-sshtest-server"
SSH_SERVER_IMAGE = "agentbox-sshtest-server:latest"
SSH_REPO = f"git@{SSH_SERVER}:/srv/git/agentbox-push-test.git"
FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "git-ssh-server"


def _wait_healthy(port: int, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError(f"container on port {port} never became healthy")


def _start_agentbox_with_ssh_key(name: str, port: int, key, known_hosts) -> str:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    subprocess.run([
        "docker", "run", "-d", "--name", name, "--network", SSH_NET,
        "-v", f"{key}:/run/secrets/deploy_key:ro",
        "-v", f"{known_hosts}:/run/secrets/known_hosts:ro",
        "-e", "AGENTBOX_GIT_SSH_KEY_FILE=/run/secrets/deploy_key",
        "-e", "AGENTBOX_GIT_KNOWN_HOSTS_FILE=/run/secrets/known_hosts",
        "-p", f"127.0.0.1:{port}:8000", _image(),
    ], capture_output=True, text=True, check=True)
    _wait_healthy(port)
    return name


@pytest.fixture(scope="module")
def ssh_server(tmp_path_factory):
    """An sshd holding a bare repo, reachable as `agentbox-sshtest-server`.

    The host key is read out of the running server itself, so known_hosts is
    built from a known-good value rather than from `ssh-keyscan`, which is
    trust-on-first-use and would defeat the point of the test.
    """
    work = tmp_path_factory.mktemp("gitssh")
    work.chmod(0o755)
    key = work / "deploy_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-f", str(key),
                    "-N", "", "-C", "agentbox-ssh-test"], check=True)
    key.chmod(0o400)          # ssh refuses a group/world readable private key

    ctx = work / "context"
    ctx.mkdir()
    shutil.copy(FIXTURE_DIR / "Dockerfile", ctx / "Dockerfile")
    shutil.copy(work / "deploy_key.pub", ctx / "authorized_keys")
    subprocess.run(["docker", "build", "-q", "-t", SSH_SERVER_IMAGE, str(ctx)],
                   capture_output=True, text=True, check=True)

    subprocess.run(["docker", "network", "create", SSH_NET], capture_output=True, check=False)
    subprocess.run(["docker", "rm", "-f", SSH_SERVER], capture_output=True, check=False)
    subprocess.run(["docker", "run", "-d", "--name", SSH_SERVER,
                    "--network", SSH_NET, SSH_SERVER_IMAGE],
                   capture_output=True, text=True, check=True)
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            logs = subprocess.run(["docker", "logs", SSH_SERVER],
                                  capture_output=True, text=True)
            if "Server listening on 0.0.0.0 port 22" in logs.stdout + logs.stderr:
                break
            time.sleep(1)
        else:
            raise RuntimeError("sshd never started")

        host_key = subprocess.run(
            ["docker", "exec", SSH_SERVER, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        good = work / "known_hosts"
        good.write_text(f"{SSH_SERVER} {host_key[0]} {host_key[1]}\n")
        good.chmod(0o444)

        # A syntactically valid known_hosts naming a key the server does not
        # have — the machine-in-the-middle case.
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-f", str(work / "other"),
                        "-N", "", "-C", "not-the-server"], check=True)
        other = (work / "other.pub").read_text().split()
        wrong = work / "known_hosts_wrong"
        wrong.write_text(f"{SSH_SERVER} {other[0]} {other[1]}\n")
        wrong.chmod(0o444)

        yield {"key": key, "known_hosts": good, "wrong_known_hosts": wrong}
    finally:
        subprocess.run(["docker", "rm", "-f", SSH_SERVER], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", SSH_NET], capture_output=True, check=False)


@pytest.fixture(scope="module")
def ssh_push_server(ssh_server):
    """AgentBox with the deploy key and the CORRECT host key mounted."""
    name = "agentbox-sshpush-test"
    try:
        yield _start_agentbox_with_ssh_key(
            name, 8066, ssh_server["key"], ssh_server["known_hosts"])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="module")
def ssh_badhost_server(ssh_server):
    """Same key, but a known_hosts that does not match the server."""
    name = "agentbox-sshbadhost-test"
    try:
        yield _start_agentbox_with_ssh_key(
            name, 8067, ssh_server["key"], ssh_server["wrong_known_hosts"])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="module")
def default_container_on_ssh_net(ssh_server):
    """The ordinary, no-credential container, able to reach the server."""
    subprocess.run(["docker", "network", "connect", SSH_NET, CONTAINER],
                   capture_output=True, check=False)
    try:
        yield CONTAINER
    finally:
        subprocess.run(["docker", "network", "disconnect", SSH_NET, CONTAINER],
                       capture_output=True, check=False)


@requires_docker_introspection
def test_ssh_push_is_enabled_and_logged(ssh_push_server):
    logs = subprocess.run(["docker", "logs", ssh_push_server],
                          capture_output=True, text=True, check=True)
    assert "Git push over SSH ENABLED" in logs.stdout + logs.stderr


@requires_docker_introspection
def test_the_container_pins_strict_host_key_checking(ssh_push_server):
    """Read the live config, not the source: this is what git will run."""
    cmd = _in(ssh_push_server, "git config --global --get core.sshCommand").stdout
    assert "StrictHostKeyChecking=yes" in cmd, cmd
    assert "IdentitiesOnly=yes" in cmd, cmd
    assert "UserKnownHostsFile=/run/secrets/known_hosts" in cmd, cmd
    assert "-i /run/secrets/deploy_key" in cmd, cmd


@requires_docker_introspection
def test_clone_over_ssh_works(ssh_push_server):
    out = _in(ssh_push_server,
              f"cd /tmp && rm -rf c1 && timeout 60 git clone -q {SSH_REPO} c1 && "
              "git -C /tmp/c1 log --oneline")
    assert "seed commit" in out.stdout, out.stdout + out.stderr


@requires_docker_introspection
def test_push_over_ssh_lands_and_a_fresh_clone_proves_it(ssh_push_server):
    """The marker is COMPUTED inside the container, so neither the push
    output nor a stale working copy can make this pass vacuously."""
    marker = _in(ssh_push_server,
                 "head -c 32 /dev/urandom | sha256sum | cut -c1-24").stdout.strip()
    assert len(marker) == 24, marker

    push = _in(ssh_push_server, f"""
        set -e
        cd /tmp && rm -rf work && timeout 60 git clone -q {SSH_REPO} work
        cd work
        git config user.email agentbox@example.invalid
        git config user.name agentbox
        echo {marker} > pushed-marker.txt
        git add pushed-marker.txt
        git commit -q -m 'ssh push test {marker}'
        timeout 60 git push -q origin main
        git rev-parse HEAD
    """)
    assert push.returncode == 0, push.stdout + push.stderr
    pushed_sha = push.stdout.strip().splitlines()[-1]

    fresh = _in(ssh_push_server,
                f"cd /tmp && rm -rf verify && timeout 60 git clone -q {SSH_REPO} verify && "
                "cat /tmp/verify/pushed-marker.txt && git -C /tmp/verify rev-parse HEAD")
    assert marker in fresh.stdout, fresh.stdout + fresh.stderr
    assert pushed_sha in fresh.stdout, fresh.stdout

    # And on the server's own bare repo, not just something the client cached.
    server = subprocess.run(
        # as `git`: the repo is owned by that user, and modern git refuses to
        # read a repo owned by someone else ("dubious ownership").
        ["docker", "exec", "-u", "git", SSH_SERVER, "git", "-C",
         "/srv/git/agentbox-push-test.git", "log", "-1", "--format=%H %s", "main"],
        capture_output=True, text=True, check=True)
    assert pushed_sha in server.stdout and marker in server.stdout, server.stdout


@requires_docker_introspection
def test_the_private_key_is_not_in_docker_inspect(ssh_push_server, ssh_server):
    body = ssh_server["key"].read_text().splitlines()[1][:40]
    inspected = subprocess.run(["docker", "inspect", ssh_push_server],
                               capture_output=True, text=True, check=True).stdout
    assert body not in inspected, "the private key leaked into docker inspect"
    assert "/run/secrets/deploy_key" in inspected


@requires_docker_introspection
def test_the_private_key_is_not_logged(ssh_push_server, ssh_server):
    body = ssh_server["key"].read_text().splitlines()[1][:40]
    logs = subprocess.run(["docker", "logs", ssh_push_server],
                          capture_output=True, text=True, check=True)
    combined = logs.stdout + logs.stderr
    assert body not in combined, "the private key was logged"
    assert "BEGIN OPENSSH PRIVATE KEY" not in combined


@requires_docker_introspection
def test_without_a_key_the_default_container_cannot_clone_or_push(default_container_on_ssh_net):
    """Off by default means off: same server, same network, no credential."""
    out = _in(default_container_on_ssh_net,
              f"cd /tmp && rm -rf nokey && timeout 60 git clone -q {SSH_REPO} nokey; "
              "echo EXIT=$?; ls -d /tmp/nokey 2>/dev/null || echo ABSENT")
    assert "EXIT=0" not in out.stdout, out.stdout + out.stderr
    assert "ABSENT" in out.stdout, out.stdout


@requires_docker_introspection
def test_a_wrong_host_key_refuses_instead_of_trusting(ssh_badhost_server):
    """The property that matters. The credential here is valid and the
    server is reachable; only the host key disagrees, and that alone must
    stop the connection rather than fall back to trusting it."""
    out = _in(ssh_badhost_server,
              f"cd /tmp && rm -rf bad && timeout 60 git clone -q {SSH_REPO} bad; "
              "echo EXIT=$?; ls -d /tmp/bad 2>/dev/null || echo ABSENT")
    combined = out.stdout + out.stderr
    assert "EXIT=0" not in combined, combined
    assert "ABSENT" in combined, combined
    assert "Host key verification failed" in combined, combined
