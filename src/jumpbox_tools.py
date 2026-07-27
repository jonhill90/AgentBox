#!/usr/bin/env python3
"""
Jumpbox tools: filesystem, git, and http_request (SPEC §8, §9).

Everything in this module is governed by the single feature toggle
`AGENTBOX_ENABLE_JUMPBOX_TOOLS` (SPEC §10). When that flag is off,
`mcp_server.py` never registers any of these with FastMCP — they do not
appear on the MCP surface at all. Importing this module is harmless on
its own; it grants nothing until something registers a call site.

Ported from Hill90 (read-only reference,
`/Users/jon/source/repos/Personal/hill90-app/services/agentbox/app/`):

  - `filesystem.py`  → `read_file`, `write_file`, `list_directory`
  - `tools.py::_execute_git`           → `execute_git`
  - `tools.py::_execute_http_request`  → `execute_http_request`
  - `tools.py::_is_blocked_host`       → `is_blocked_host`

Two things were dropped in the port because they are Hill90 platform
concepts that do not exist in this standalone repo: the `EventEmitter`
telemetry hooks, and `FilesystemConfig`-driven `configure()` (the path
policy here is fixed to the workspace at import instead of being wired
from a config object).

Every function returns a JSON *string*, matching this repo's existing
tool convention, and reports failure as `{"success": false, "error": ...}`
rather than raising.
"""

import asyncio
import ipaddress
import json
import os
import socket
import urllib.parse

from policy import WORKSPACE_ROOT, PathPolicy

# The one directory these tools may touch. Not "/", not /app.
WORKSPACE = WORKSPACE_ROOT

# Scoped at import: read/write allowed under the workspace and nowhere
# else. Deliberately not configurable by environment — widening the
# blast radius of the filesystem tools should be a reviewed code change,
# not a deployment-time env var someone can set by accident.
_policy = PathPolicy(allowed_paths=[WORKSPACE], denied_paths=[], read_only=False)

MAX_READ_BYTES = 1_000_000       # 1MB, as Hill90
MAX_HTTP_RESPONSE_CHARS = 50_000  # as Hill90
MAX_HTTP_RESPONSE_BYTES = 2_000_000  # hard read cap, so a huge body cannot OOM us
MAX_REDIRECTS = 3                 # as Hill90; each hop is re-checked, see below


# ── Filesystem (SPEC §8, from Hill90 filesystem.py) ──────────────────

async def read_file(path: str) -> str:
    """Read file contents with path policy enforcement."""
    allowed, reason = _policy.check_read(path)
    if not allowed:
        return json.dumps({"success": False, "error": reason})

    try:
        with open(path) as f:
            content = f.read(MAX_READ_BYTES)
        return json.dumps({"success": True, "content": content, "path": path})
    except FileNotFoundError:
        return json.dumps({"success": False, "error": f"File not found: {path}"})
    except IsADirectoryError:
        return json.dumps({"success": False, "error": f"Not a file: {path}"})
    except PermissionError:
        return json.dumps({"success": False, "error": f"Permission denied: {path}"})
    except UnicodeDecodeError:
        return json.dumps({"success": False, "error": f"Not a UTF-8 text file: {path}"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})


async def write_file(path: str, content: str) -> str:
    """Write content to a file with path policy enforcement."""
    allowed, reason = _policy.check_write(path)
    if not allowed:
        return json.dumps({"success": False, "error": reason})

    # Operate on the RESOLVED path. check_write validates realpath(path), but
    # os.makedirs walks the literal string and recurses through ".."
    # components, materialising every intermediate directory — so a path whose
    # realpath is inside /workspace could still mkdir -p anywhere this uid can
    # write, including $HOME where the shell's startup files live.
    path = os.path.realpath(path)

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return json.dumps({"success": True, "path": path, "bytes_written": len(content)})
    except IsADirectoryError:
        return json.dumps({"success": False, "error": f"Not a file: {path}"})
    except PermissionError:
        return json.dumps({"success": False, "error": f"Permission denied: {path}"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})


async def list_directory(path: str) -> str:
    """List directory contents with path policy enforcement."""
    allowed, reason = _policy.check_read(path)
    if not allowed:
        return json.dumps({"success": False, "error": reason})

    try:
        entries = []
        for entry in sorted(os.listdir(path)):
            full_path = os.path.join(path, entry)
            try:
                stat = os.stat(full_path)
                entries.append({
                    "name": entry,
                    "type": "directory" if os.path.isdir(full_path) else "file",
                    "size": stat.st_size,
                })
            except OSError:
                entries.append({"name": entry, "type": "unknown", "size": 0})
        return json.dumps({"success": True, "path": path, "entries": entries})
    except FileNotFoundError:
        return json.dumps({"success": False, "error": f"Directory not found: {path}"})
    except NotADirectoryError:
        return json.dumps({"success": False, "error": f"Not a directory: {path}"})
    except PermissionError:
        return json.dumps({"success": False, "error": f"Permission denied: {path}"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)[:300]})


# ── git (SPEC §8, from Hill90 tools.py::_execute_git) ────────────────
#
# A fixed set of subcommands scoped to the workspace — NOT a general git
# passthrough. There is deliberately no `git <arbitrary args>` path: if a
# subcommand is not in this list, it is not supported. Adding one is a
# reviewed change, not a parameter.

GIT_ACTIONS = ("init", "status", "add", "commit", "diff", "log", "reset")

GIT_USER_NAME = os.environ.get("AGENTBOX_GIT_USER_NAME", "AgentBox")
GIT_USER_EMAIL = os.environ.get("AGENTBOX_GIT_USER_EMAIL", "agentbox@localhost")


async def _git(*argv: str) -> tuple[int, str, str]:
    """Run one git invocation in the workspace. Never uses a shell."""
    proc = await asyncio.create_subprocess_exec(
        "git", *argv, cwd=WORKSPACE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, (stdout or b"").decode().strip(), (stderr or b"").decode().strip()


async def execute_git(action: str, paths: str = ".", message: str = "", count: int = 10) -> str:
    """Execute one of the fixed git subcommands in the workspace."""
    os.makedirs(WORKSPACE, exist_ok=True)

    # Auto-init on first use so the other subcommands work without an
    # explicit init call (Hill90 does the same).
    if not os.path.isdir(os.path.join(WORKSPACE, ".git")) and action != "init":
        for argv in (
            ("init",),
            ("config", "user.name", GIT_USER_NAME),
            ("config", "user.email", GIT_USER_EMAIL),
        ):
            await _git(*argv)

    try:
        if action == "init":
            outputs = []
            for argv in (
                ("init",),
                ("config", "user.name", GIT_USER_NAME),
                ("config", "user.email", GIT_USER_EMAIL),
            ):
                _rc, out, _err = await _git(*argv)
                outputs.append(out)
            return json.dumps({
                "success": True,
                "output": "\n".join(filter(None, outputs)) or "Git repo initialized",
            })

        elif action == "status":
            _rc, out, _err = await _git("status", "--short")
            return json.dumps({"success": True, "output": out or "Working tree clean"})

        elif action == "add":
            targets = paths.split() or ["."]
            rc, _out, err = await _git("add", "--", *targets)
            if rc != 0:
                return json.dumps({"success": False, "error": err})
            return json.dumps({"success": True, "output": f"Staged: {' '.join(targets)}"})

        elif action == "commit":
            if not message:
                return json.dumps({"success": False, "error": "message is required for commit"})
            rc, out, err = await _git("commit", "-m", message)
            if rc != 0:
                if "nothing to commit" in err or "nothing to commit" in out:
                    return json.dumps({"success": True, "output": "Nothing to commit"})
                return json.dumps({"success": False, "error": err or out})
            return json.dumps({"success": True, "output": out})

        elif action == "diff":
            _rc, out, _err = await _git("diff", "--stat")
            return json.dumps({"success": True, "output": out or "No changes"})

        elif action == "log":
            n = min(max(int(count or 10), 1), 50)
            rc, out, err = await _git("log", "--oneline", f"-{n}")
            if rc != 0:
                if "does not have any commits" in err:
                    return json.dumps({"success": True, "output": "No commits yet"})
                return json.dumps({"success": False, "error": err})
            return json.dumps({"success": True, "output": out or "No commits yet"})

        elif action == "reset":
            targets = paths.split() or ["."]
            _rc, out, _err = await _git("reset", "HEAD", "--", *targets)
            return json.dumps({"success": True, "output": out or "Unstaged"})

        else:
            return json.dumps({"success": False, "error": f"Unknown git action: {action}"})

    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)[:300]})


# ── http_request (SPEC §9, from Hill90 tools.py) ─────────────────────
#
# SSRF protection: resolve the hostname and check the *resolved IP* against
# the blocklist, rather than string-matching the URL.
#
# KNOWN LIMITATION, do not overstate this: the check and the connection
# resolve DNS independently, so a TTL-0 record answering public once and
# private the second time still gets through (classic rebinding TOCTOU). The
# complete fix is to resolve once and pin the connection to the vetted
# address with the hostname carried in Host/SNI. Tracked in docs/architecture/security.md.

# CGNAT is the only range Python's ipaddress does not already classify, and
# it is the one Tailscale uses. Everything else is covered by the property
# checks in _addr_blocked, which are exhaustive in a way a hand-maintained
# CIDR list is not — the previous list silently omitted 0.0.0.0/8 (which
# reaches 127.0.0.1 on Linux) and every IPv6 range.
_EXTRA_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT — the Tailscale range
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),    # benchmarking
]


# Tests set this to reach a redirect server on loopback. It exists so the
# suite can lift EXACTLY loopback and nothing else, and so the lift is
# visible in the source rather than achieved by monkeypatching internals.
ALLOW_LOOPBACK = False


def _addr_blocked(addr) -> bool:
    """True if this address must never be reached."""
    if ALLOW_LOOPBACK and addr.is_loopback:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped          # ::ffff:127.0.0.1 is loopback
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return True
    return any(addr in cidr for cidr in _EXTRA_BLOCKED
               if cidr.version == addr.version)


def resolve_allowed(hostname: str) -> str | None:
    """Return one vetted IP for `hostname`, or None if any answer is blocked.

    Returning the address is what closes the rebinding TOCTOU: the caller
    connects to THIS literal, so a second DNS answer cannot redirect it. If
    any returned address is blocked the whole name is refused — a host that
    answers with one public and one private address is not safe to reach.
    """
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    vetted = None
    for _family, _t, _p, _c, sockaddr in addrs:
        try:
            addr = ipaddress.ip_address(sockaddr[0].split("%")[0])
        except ValueError:
            return None
        if _addr_blocked(addr):
            return None
        if vetted is None:
            vetted = addr
    return str(vetted) if vetted else None


def is_blocked_host(hostname: str) -> bool:
    """True if the hostname resolves into a blocked range (or not at all).

    Resolves AF_UNSPEC, not AF_INET. httpx resolves both families and prefers
    IPv6 per RFC 6724, so checking only A records let a name with a public A
    and a private AAAA through to the private address.
    """
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return True  # Can't resolve (or the name is malformed) — block
    for family, _, _, _, sockaddr in addrs:
        try:
            addr = ipaddress.ip_address(sockaddr[0].split("%")[0])
        except ValueError:
            return True
        if _addr_blocked(addr):
            return True
    return False


def _pin(request):
    """Rewrite a request to connect to a vetted IP, or None if blocked.

    The host goes in the Host header (and SNI for TLS) while the URL carries
    the literal address, so the connection cannot be re-pointed by a second
    DNS answer between the check and the connect.
    """
    hostname = request.url.host
    ip = resolve_allowed(hostname)
    if ip is None:
        return None
    if ALLOW_LOOPBACK and ipaddress.ip_address(ip).is_loopback:
        return request      # tests drive a real loopback server by name
    request.headers["Host"] = request.url.netloc.decode("ascii")
    literal = f"[{ip}]" if ":" in ip else ip
    request.url = request.url.copy_with(host=literal)
    request.extensions = dict(request.extensions or {})
    request.extensions["sni_hostname"] = hostname
    return request


async def execute_http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str = "",
) -> str:
    """Make an outbound HTTP request with SSRF protection."""
    import httpx

    method = (method or "GET").upper()
    headers = headers or {}

    if method not in ("GET", "POST"):
        return json.dumps({"success": False, "error": "method must be GET or POST"})
    if not url or not url.startswith(("http://", "https://")):
        return json.dumps({"success": False, "error": "url must start with http:// or https://"})

    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    if not hostname:
        return json.dumps({"success": False, "error": "Invalid URL"})

    # NETWORK_ALLOWLIST (SPEC §7) is where a future reviewed exception to
    # this blocklist would be checked — before the rejection below, never
    # by removing an entry from _BLOCKED_CIDRS. No exception exists yet,
    # so there is deliberately no carve-out code here to go stale.
    if is_blocked_host(hostname):
        return json.dumps({"success": False, "error": "Blocked: internal/private IP range"})

    try:
        # Redirects are followed manually, one hop at a time, so every hop's
        # host goes through the same blocklist. Letting httpx follow them
        # would check only the URL we were handed: a public URL that 302s to
        # http://169.254.169.254/ would sail straight through to the metadata
        # service. A blocked hop stops the chain and returns the same
        # structured refusal as a directly-blocked request.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            request = _pin(client.build_request(
                method, url, headers=headers, content=(body if method == "POST" else None)
            ))
            if request is None:
                return json.dumps({
                    "success": False, "error": "Blocked: internal/private IP range"})

            for _hop in range(MAX_REDIRECTS + 1):
                # Bound the body: res.text would buffer the whole response
                # first, so a hostile endpoint could OOM us before we
                # truncated anything.
                res = await client.send(request, stream=True)
                try:
                    body_bytes = b""
                    async for chunk in res.aiter_bytes():
                        body_bytes += chunk
                        if len(body_bytes) > MAX_HTTP_RESPONSE_BYTES:
                            break
                finally:
                    await res.aclose()

                if not res.has_redirect_location:
                    truncated = len(body_bytes) > MAX_HTTP_RESPONSE_BYTES
                    text = body_bytes[:MAX_HTTP_RESPONSE_BYTES].decode(
                        res.encoding or "utf-8", errors="replace")
                    return json.dumps({
                        "success": True,
                        "status": res.status_code,
                        "headers": dict(list(res.headers.items())[:20]),
                        "body": text[:MAX_HTTP_RESPONSE_CHARS],
                        "truncated": truncated or len(text) > MAX_HTTP_RESPONSE_CHARS,
                    })

                # httpx builds the next request for us (including the
                # POST->GET downgrade on 301/302/303), we just vet it.
                nxt = res.next_request
                if nxt is None:
                    return json.dumps({"success": False, "error": "Invalid redirect"})

                if nxt.url.scheme not in ("http", "https"):
                    return json.dumps({
                        "success": False,
                        "error": f"Blocked: redirect to non-HTTP scheme '{nxt.url.scheme}'",
                    })

                hop_host = nxt.url.host or ""
                pinned = _pin(nxt) if hop_host else None
                if pinned is None:
                    return json.dumps({
                        "success": False,
                        "error": "Blocked: redirect to internal/private IP range",
                    })

                request = pinned

            return json.dumps({
                "success": False,
                "error": f"Too many redirects (limit {MAX_REDIRECTS})",
            })
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)[:300]})
