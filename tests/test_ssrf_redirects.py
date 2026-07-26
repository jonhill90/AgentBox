#!/usr/bin/env python3
"""
SSRF protection across redirect hops.

Checking only the URL the caller hands us is not enough: a public URL
that 302s to http://169.254.169.254/ would reach the metadata service.
`execute_http_request` therefore follows redirects manually and runs
every hop's host through the same blocklist before following it.

These tests import the module directly and drive a throwaway redirect
server on loopback — no container needed, no network needed. To make
that possible the loopback range is temporarily lifted from the
blocklist (so the *entry* URL is reachable) while the redirect target
stays firmly inside a blocked range. That is the only thing patched;
the code under test is untouched.
"""

import ipaddress
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import jumpbox_tools  # noqa: E402


class _RedirectHandler(BaseHTTPRequestHandler):
    """Redirects wherever the query string says, or serves a plain body."""

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.startswith("/redirect-to?"):
            target = self.path.split("=", 1)[1]
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
        elif self.path.startswith("/loop"):
            # Endless self-redirect, for the hop-limit test.
            self.send_response(302)
            self.send_header("Location", f"{self.server.base_url}/loop")
            self.end_headers()
        elif self.path.startswith("/chain"):
            # Bounces back to this same server, then off to a blocked host.
            hop = int(self.path.rsplit("/", 1)[-1] or 0)
            self.send_response(302)
            if hop >= 2:
                self.send_header("Location", "http://10.0.0.1/final")
            else:
                self.send_header("Location", f"{self.server.base_url}/chain/{hop + 1}")
            self.end_headers()
        else:
            payload = b"landed on the origin server"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    do_POST = do_GET

    def log_message(self, *_args):
        pass  # keep pytest output clean


@pytest.fixture(scope="module")
def redirect_server():
    server = HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.base_url
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def loopback_reachable(monkeypatch):
    """Lift ONLY 127.0.0.0/8, so the test server itself is a valid entry point.

    Every other blocked range — crucially the redirect targets used below —
    stays in place.
    """
    without_loopback = [
        cidr for cidr in jumpbox_tools._BLOCKED_CIDRS
        if cidr != ipaddress.ip_network("127.0.0.0/8")
    ]
    monkeypatch.setattr(jumpbox_tools, "_BLOCKED_CIDRS", without_loopback)


async def test_the_test_server_is_reachable_when_loopback_is_lifted(
    redirect_server, loopback_reachable
):
    """Sanity check: without this, every assertion below would pass vacuously."""
    result = json.loads(await jumpbox_tools.execute_http_request(f"{redirect_server}/plain"))
    assert result["success"], result
    assert result["status"] == 200, result
    assert "landed on the origin server" in result["body"], result


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata
    "http://10.0.0.1/admin",                      # RFC1918
    "http://192.168.1.1/",                        # RFC1918
    "http://100.64.0.1/",                         # CGNAT / Tailscale
])
async def test_redirect_into_a_blocked_range_is_refused(
    redirect_server, loopback_reachable, target
):
    result = json.loads(await jumpbox_tools.execute_http_request(
        f"{redirect_server}/redirect-to?url={target}"
    ))
    assert result["success"] is False, f"followed a redirect to {target}: {result}"
    assert result["error"] == "Blocked: redirect to internal/private IP range", result


async def test_blocked_redirect_returns_the_same_shape_as_a_direct_block(
    redirect_server, loopback_reachable
):
    """A refusal, not a raw exception leaking out of httpx."""
    redirected = json.loads(await jumpbox_tools.execute_http_request(
        f"{redirect_server}/redirect-to?url=http://10.0.0.1/"
    ))
    direct = json.loads(await jumpbox_tools.execute_http_request("http://10.0.0.1/"))

    assert set(redirected) == set(direct) == {"success", "error"}
    assert redirected["success"] is direct["success"] is False
    assert "Blocked" in redirected["error"] and "Blocked" in direct["error"]


async def test_redirect_to_a_non_http_scheme_is_refused(redirect_server, loopback_reachable):
    result = json.loads(await jumpbox_tools.execute_http_request(
        f"{redirect_server}/redirect-to?url=file:///etc/passwd"
    ))
    assert result["success"] is False, result
    assert "non-HTTP scheme" in result["error"], result


async def test_a_blocked_hop_late_in_a_chain_is_still_caught(
    redirect_server, loopback_reachable
):
    """The check is per hop, not just on the first redirect."""
    result = json.loads(await jumpbox_tools.execute_http_request(f"{redirect_server}/chain/0"))
    assert result["success"] is False, result
    assert "Blocked" in result["error"], result


async def test_redirect_chains_are_still_bounded(redirect_server, loopback_reachable, monkeypatch):
    """A redirect loop ends in a structured error, not a hang."""
    monkeypatch.setattr(jumpbox_tools, "MAX_REDIRECTS", 2)
    result = json.loads(await jumpbox_tools.execute_http_request(f"{redirect_server}/loop"))
    assert result["success"] is False, result
    assert "Too many redirects" in result["error"], result


async def test_allowed_redirects_still_work(redirect_server, loopback_reachable):
    """Re-checking hops must not break ordinary redirects."""
    result = json.loads(await jumpbox_tools.execute_http_request(
        f"{redirect_server}/redirect-to?url={redirect_server}/plain"
    ))
    assert result["success"], result
    assert result["status"] == 200, result
    assert "landed on the origin server" in result["body"], result


async def test_entry_url_in_a_blocked_range_is_still_refused_first(redirect_server):
    """With the real blocklist in place, loopback never gets a first hop."""
    result = json.loads(await jumpbox_tools.execute_http_request(f"{redirect_server}/plain"))
    assert result["success"] is False, result
    assert result["error"] == "Blocked: internal/private IP range", result
