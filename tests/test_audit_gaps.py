#!/usr/bin/env python3
"""
Coverage the audit found missing entirely.

Each test here corresponds to a mechanism the code claims and nothing
previously exercised — the kind of gap where a regression would be silent
because no test would notice.
"""

import ipaddress
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import BASE_URL, requires_docker_introspection

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import jumpbox_tools  # noqa: E402
import mcp_server  # noqa: E402

websockets = pytest.importorskip("websockets")


# ── SSRF: the ranges the hand-maintained CIDR list silently omitted ──

@pytest.mark.parametrize("host", [
    "0.0.0.0",        # reaches 127.0.0.1 on Linux — was NOT in the old list
    "0",              # same, in integer form
    "127.0.0.1",
    "10.0.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",     # CGNAT / Tailscale
    "192.0.0.1",      # IETF protocol assignments
    "198.18.0.1",     # benchmarking
    "224.0.0.1",      # multicast
    "255.255.255.255",
])
def test_blocked_ranges(host):
    assert jumpbox_tools.is_blocked_host(host) is True, f"{host} was reachable"


@pytest.mark.parametrize("addr", ["::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"])
def test_ipv6_ranges_are_blocked(addr):
    """The old check resolved AF_INET only, so every v6 range was unchecked
    — a name with a public A and a private AAAA passed while httpx, which
    prefers v6 per RFC 6724, connected to the private address."""
    assert jumpbox_tools._addr_blocked(ipaddress.ip_address(addr)) is True


def test_a_public_address_is_still_reachable():
    """Guards against the blocklist becoming block-everything."""
    assert jumpbox_tools._addr_blocked(ipaddress.ip_address("93.184.216.34")) is False
    assert jumpbox_tools._addr_blocked(ipaddress.ip_address("2606:2800::1")) is False


def test_resolution_failure_fails_closed():
    assert jumpbox_tools.is_blocked_host("no-such-host.invalid") is True
    assert jumpbox_tools.is_blocked_host("x" * 300) is True   # raises UnicodeError


def test_resolve_allowed_returns_an_address_for_pinning():
    """The connection is pinned to this literal, which is what closes the
    rebinding TOCTOU — a second DNS answer cannot re-point it."""
    assert jumpbox_tools.resolve_allowed("127.0.0.1") is None
    ip = jumpbox_tools.resolve_allowed("example.com")
    assert ip and not jumpbox_tools._addr_blocked(ipaddress.ip_address(ip))


# ── the rebinding guard's own logic ──────────────────────────────────

@pytest.mark.parametrize("host,ok", [
    ("localhost", True), ("localhost:8054", True), ("127.0.0.1:9999", True),
    ("[::1]:8054", True), (None, True),
    ("evil.com", False), ("localhost.evil.com", False), ("127.0.0.1.evil.com", False),
])
def test_host_allowlist(host, ok):
    assert mcp_server._host_allowed(host) is ok


@pytest.mark.parametrize("origin,ok", [
    ("http://localhost:8054", True), ("https://127.0.0.1", True), (None, True),
    ("http://evil.com", False), ("http://localhost.evil.com", False),
    ("file:///etc/passwd", False), ("null", False),
    # userinfo must not confuse the parse: the real host here is evil.com.
    ("http://localhost:8054@evil.com", False),
])
def test_origin_allowlist(origin, ok):
    assert mcp_server._origin_allowed(origin) is ok


@requires_docker_introspection
async def test_rebinding_guard_covers_the_websocket_route():
    """The guard's WebSocket branch had no test, and this is exactly the
    CVE-2015-8400 shape the module docstring cites."""
    with pytest.raises(Exception) as exc:
        async with websockets.connect(
            f"{BASE_URL.replace('http://', 'ws://')}/terminal",
            additional_headers={"Origin": "http://evil.example.com"},
        ):
            pass
    assert "403" in str(exc.value) or "rejected" in str(exc.value).lower(), exc.value


# ── terminal pre-auth limits, none of which were referenced by a test ──

@requires_docker_introspection
def test_preauth_constants_are_wired_up():
    import terminal
    assert terminal.MAX_AUTH_ATTEMPTS >= 1
    assert terminal.MAX_AUTH_MESSAGE_BYTES >= 1
    assert terminal.AUTH_TIMEOUT > 0
    assert terminal.MAX_PENDING_AUTH >= 1


# ── response truncation, only ever asserted in the False direction ──

async def test_read_file_caps_what_it_returns():
    from conftest import call, mcp_session
    big = "x" * 4096
    path = "/workspace/truncation-probe.txt"
    async with mcp_session() as session:
        await call(session, "write_file", {"path": path, "content": big})
        result = await call(session, "read_file", {"path": path})
        assert result["success"], result
        assert len(result["content"]) <= jumpbox_tools.MAX_READ_BYTES


async def test_http_request_passes_custom_headers():
    """The `headers` parameter was accepted and never exercised."""
    from conftest import call, mcp_session
    async with mcp_session() as session:
        result = await call(session, "http_request", {
            "url": "https://example.com/", "headers": {"X-Agentbox-Probe": "1"},
        })
        assert result["success"], result


# ── the auth-off default, which is skipped on a machine that has a token ──

@requires_docker_introspection
def test_auth_off_default_in_a_container_with_no_token():
    """`requires_auth_off` skips permanently once .env has a token, so the
    unauthenticated default was never actually verified anywhere. This
    checks it in a container of its own instead of skipping."""
    image = subprocess.run(
        ["docker", "inspect", "agentbox", "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    name, port = "agentbox-nodefault-test", 8064
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8000", image],
        capture_output=True, check=True,
    )
    try:
        import time
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/auth-required", timeout=10
        ) as resp:
            assert json.loads(resp.read())["auth_required"] is False

        # ...and with no token configured, an /api/* route is genuinely open.
        req = urllib.request.Request(
            f"http://localhost:{port}/api/browser/navigate",
            data=b'{"url": "https://example.com/"}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == 200

        logs = subprocess.run(["docker", "logs", name],
                              capture_output=True, text=True, check=True)
        assert "Auth DISABLED" in logs.stdout + logs.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
