#!/usr/bin/env python3
"""
Hardening from the auth research pass: DNS-rebinding guard, file-based
secrets, and rotation overlap.

Each of these closes a specific, cited defect:

  - Origin/Host validation — MCP transports spec ("Servers MUST validate
    the Origin header"), RFC 6455 §10.2, and CVE-2015-8400, where DNS
    rebinding defeated "it only listens on localhost".
  - AGENTBOX_AUTH_TOKEN_FILE — Docker's own guidance against secrets in
    environment variables, which are returned verbatim by `docker inspect`.
  - Two simultaneously-valid tokens — without an overlap window, rotation
    needs downtime and coordinated client reconfiguration, so it never
    actually happens.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from conftest import BASE_URL, CONTAINER, requires_docker_introspection

TOKEN_A = "hardening-token-current-aaa"
TOKEN_B = "hardening-token-previous-bbb"


def _raw_request(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _wait_for(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    raise RuntimeError(f"port {port} never became healthy")


# ── DNS rebinding guard ──────────────────────────────────────────────

async def test_health_still_works_without_an_origin():
    """Non-browser callers omit Origin entirely; that must stay allowed."""
    status, _ = _raw_request(f"{BASE_URL}/health")
    assert status == 200, status


async def test_same_origin_request_is_allowed():
    status, _ = _raw_request(f"{BASE_URL}/health", {"Origin": BASE_URL})
    assert status == 200, status


@pytest.mark.parametrize("origin", [
    "http://evil.example.com",
    "https://attacker.test",
    "http://localhost.evil.com",     # substring of an allowed name
    "http://127.0.0.1.evil.com",     # ditto
])
async def test_foreign_origin_is_blocked(origin):
    """Substring-alike origins must fail — OWASP: allowlist, not denylist."""
    status, body = _raw_request(f"{BASE_URL}/health", {"Origin": origin})
    assert status == 403, f"{origin} -> {status}"
    assert "Forbidden" in body, body


async def test_foreign_host_is_blocked():
    """The DNS-rebinding case: attacker DNS resolves to 127.0.0.1, so the
    packet arrives, but the Host header still names their domain."""
    status, body = _raw_request(f"{BASE_URL}/health", {"Host": "rebind.evil.com"})
    assert status == 403, f"{status} {body}"


async def test_allowed_hosts_accept_any_port():
    status, _ = _raw_request(f"{BASE_URL}/health", {"Host": "localhost:9999"})
    assert status == 200, status


async def test_the_guard_covers_api_routes_not_just_health():
    status, _ = _raw_request(f"{BASE_URL}/api/auth-required",
                             {"Origin": "http://evil.example.com"})
    assert status == 403, status


# ── file-based secrets + rotation overlap ────────────────────────────

@pytest.fixture(scope="module")
def token_file_server(tmp_path_factory):
    """A container whose token comes from a FILE, with a previous token set."""
    image = subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    secret_dir = tmp_path_factory.mktemp("secrets")
    secret = secret_dir / "token"
    secret.write_text(TOKEN_A + "\n")   # trailing newline must be tolerated
    secret_dir.chmod(0o755)
    secret.chmod(0o644)

    name, port = "agentbox-tokenfile-test", 8063
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    subprocess.run([
        "docker", "run", "-d", "--name", name,
        "-v", f"{secret}:/run/secrets/agentbox_token:ro",
        "-e", "AGENTBOX_AUTH_TOKEN_FILE=/run/secrets/agentbox_token",
        "-e", f"AGENTBOX_AUTH_TOKEN_PREVIOUS={TOKEN_B}",
        "-p", f"{port}:8000", image,
    ], capture_output=True, text=True, check=True)
    try:
        _wait_for(port)
        yield f"http://localhost:{port}", name
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@requires_docker_introspection
def test_token_read_from_file_is_accepted(token_file_server):
    url, _ = token_file_server
    status, body = _raw_request(f"{url}/api/auth-required")
    assert json.loads(body)["auth_required"] is True, body

    status, _ = _raw_request(f"{url}/api/screenshot",
                             {"Authorization": f"Bearer {TOKEN_A}"})
    assert status != 401, status  # 404 just means no browser yet; 401 is the failure


@requires_docker_introspection
def test_the_previous_token_is_also_accepted(token_file_server):
    """The rotation overlap: both work while both are configured."""
    url, _ = token_file_server
    status, _ = _raw_request(f"{url}/api/screenshot",
                             {"Authorization": f"Bearer {TOKEN_B}"})
    assert status != 401, status


@requires_docker_introspection
def test_an_unrelated_token_is_still_rejected(token_file_server):
    url, _ = token_file_server
    status, _ = _raw_request(f"{url}/api/screenshot",
                             {"Authorization": "Bearer neither-of-them"})
    assert status == 401, status


@requires_docker_introspection
def test_the_secret_is_not_exposed_in_docker_inspect(token_file_server):
    """The entire point of the file form: the env holds a path, not a secret."""
    _url, name = token_file_server
    env = subprocess.run(
        ["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert TOKEN_A not in env, "the secret leaked into docker inspect output"
    assert "/run/secrets/agentbox_token" in env, env


@requires_docker_introspection
def test_startup_logs_a_fingerprint_not_the_secret(token_file_server):
    _url, name = token_file_server
    logs = subprocess.run(["docker", "logs", name],
                          capture_output=True, text=True, check=True)
    combined = logs.stdout + logs.stderr
    assert TOKEN_A not in combined, "the secret was logged"
    assert "fingerprint" in combined, combined[-600:]
    assert "rotation" in combined, "the previous-token state is not visible in logs"
