#!/usr/bin/env python3
"""
Interactive terminal (SPEC.md §15 / PRD 1.11).

The terminal is off by default, so the whole file works against extra
containers started with the toggle flipped — same pattern as the §10 and
§12 tests. Three configurations get exercised, because the interesting
behaviour is in the combinations:

  - toggle off              -> no route exists at all
  - toggle on, no token     -> fail closed, every connection refused
  - toggle on, token set    -> works, and only with the right token

The test that actually proves the port works is the echo round-trip:
write a command into the PTY, read its output back. Everything else is
the gate.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from conftest import CONTAINER, requires_docker_introspection

websockets = pytest.importorskip(
    "websockets", reason="pip install websockets to run the terminal tests"
)

TOKEN = "terminal-test-token-9f8e7d"
WRONG_TOKEN = "terminal-test-token-wrong"

# Three throwaway containers, one per configuration.
CONFIGS = {
    "off":        (8060, {"AGENTBOX_ENABLE_TERMINAL": "false", "AGENTBOX_AUTH_TOKEN": TOKEN}),
    "no_token":   (8061, {"AGENTBOX_ENABLE_TERMINAL": "true"}),
    "on":         (8062, {"AGENTBOX_ENABLE_TERMINAL": "true", "AGENTBOX_AUTH_TOKEN": TOKEN}),
}


def _wait_for(port: int, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = exc
        time.sleep(1)
    raise RuntimeError(f"port {port} never became healthy: {last}")


@pytest.fixture(scope="module")
def servers():
    """Start one container per configuration; tear them all down after."""
    image = subprocess.run(
        ["docker", "inspect", CONTAINER, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    names = {}
    try:
        for key, (port, env) in CONFIGS.items():
            name = f"agentbox-terminal-{key}"
            names[key] = (name, port)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
            cmd = ["docker", "run", "-d", "--name", name]
            for var, value in env.items():
                cmd += ["-e", f"{var}={value}"]
            cmd += ["-p", f"{port}:8000", image]
            subprocess.run(cmd, capture_output=True, text=True, check=True)

        for _key, (_name, port) in names.items():
            _wait_for(port)

        yield {key: port for key, (_name, port) in names.items()}
    finally:
        for name, _port in names.values():
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _assert_handshake_rejected(exc) -> None:
    """An auth refusal shows up as an HTTP 403, not close code 4001.

    terminal.py closes the socket BEFORE accept(), so uvicorn never
    completes the upgrade and the client is rejected at the handshake.
    The 4001 close code is therefore unobservable — which is fine, and
    strictly safer than accepting a socket in order to close it politely.
    See the note in src/terminal.py.
    """
    text = str(exc)
    assert "403" in text or "rejected" in text.lower(), f"unexpected rejection: {exc!r}"


def _ws_url(port: int, token: str | None = None) -> str:
    url = f"ws://localhost:{port}/terminal"
    return f"{url}?token={token}" if token else url


async def _drain(ws, seconds: float = 3.0) -> bytes:
    """Collect whatever the PTY emits within a window."""
    out = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            remaining = max(0.05, deadline - time.time())
            frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            break
        except Exception:
            break
        out += frame if isinstance(frame, bytes) else frame.encode()
    return out


import asyncio  # noqa: E402  (used by _drain above)


# ── toggle off: the route does not exist ─────────────────────────────

@requires_docker_introspection
async def test_no_terminal_route_when_toggle_is_off(servers):
    """Not a refusal from a live endpoint — nothing to connect to."""
    with pytest.raises(Exception) as exc:
        async with websockets.connect(_ws_url(servers["off"], TOKEN)):
            pass
    # Starlette answers an unrouted ws path with a 403 handshake rejection.
    assert "403" in str(exc.value) or "rejected" in str(exc.value).lower(), exc.value


@requires_docker_introspection
def test_toggle_off_logs_that_no_route_was_registered(servers):
    logs = subprocess.run(
        ["docker", "logs", "agentbox-terminal-off"],
        capture_output=True, text=True, check=True,
    )
    assert "Terminal DISABLED" in logs.stdout + logs.stderr


@requires_docker_introspection
def test_toggle_off_does_not_disturb_the_rest_of_the_server(servers):
    with urllib.request.urlopen(f"http://localhost:{servers['off']}/health", timeout=10) as resp:
        assert json.loads(resp.read())["status"] == "healthy"


# ── toggle on, no token: fail closed ─────────────────────────────────

@requires_docker_introspection
async def test_fail_closed_when_no_token_is_configured(servers):
    """SPEC §15.3: the one place "auth off" must NOT mean "open"."""
    with pytest.raises(Exception) as exc:
        async with websockets.connect(_ws_url(servers["no_token"], "anything")):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
async def test_fail_closed_even_with_no_token_supplied(servers):
    with pytest.raises(Exception):
        async with websockets.connect(_ws_url(servers["no_token"])):
            pass


@requires_docker_introspection
def test_fail_closed_config_warns_loudly_at_startup(servers):
    logs = subprocess.run(
        ["docker", "logs", "agentbox-terminal-no_token"],
        capture_output=True, text=True, check=True,
    )
    combined = logs.stdout + logs.stderr
    assert "Terminal ENABLED" in combined
    assert "fail-closed" in combined, combined[-1500:]


# ── toggle on, token set: the gate ───────────────────────────────────

@requires_docker_introspection
async def test_missing_token_is_refused(servers):
    with pytest.raises(Exception) as exc:
        async with websockets.connect(_ws_url(servers["on"])):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
async def test_wrong_token_is_refused(servers):
    with pytest.raises(Exception) as exc:
        async with websockets.connect(_ws_url(servers["on"], WRONG_TOKEN)):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
async def test_right_token_connects(servers):
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        assert ws.state.name in ("OPEN", "CONNECTING"), ws.state


# ── the actual port: a real shell round-trip ─────────────────────────

@requires_docker_introspection
async def test_echo_round_trip_through_the_pty(servers):
    """The proof the relay works, equivalent to navigate-then-screenshot."""
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        await _drain(ws, 2.0)  # let the shell/tmux draw its prompt

        await ws.send(b"echo agentbox-terminal-works\n")
        output = await _drain(ws, 6.0)

        assert b"agentbox-terminal-works" in output, output[-400:]


@requires_docker_introspection
async def test_shell_sees_the_workspace(servers):
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        await _drain(ws, 2.0)
        await ws.send(b"pwd\n")
        output = await _drain(ws, 6.0)
        assert b"/workspace" in output, output[-400:]


@requires_docker_introspection
async def test_resize_control_frame_reaches_the_shell(servers):
    """A JSON text frame must move the child's idea of the terminal size."""
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        await _drain(ws, 2.0)

        await ws.send(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        await asyncio.sleep(0.5)

        await ws.send(b"tput cols\n")
        output = await _drain(ws, 6.0)
        assert b"100" in output, output[-400:]


@requires_docker_introspection
async def test_unknown_control_frames_are_ignored(servers):
    """Hill90's client sends {"type":"ping"}; the server must tolerate it."""
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        await _drain(ws, 2.0)

        await ws.send(json.dumps({"type": "ping"}))
        await ws.send("not json at all")
        await asyncio.sleep(0.3)

        # Still alive and still relaying.
        await ws.send(b"echo still-here\n")
        output = await _drain(ws, 6.0)
        assert b"still-here" in output, output[-400:]


@requires_docker_introspection
async def test_the_shell_does_not_inherit_the_auth_token(servers):
    """The server process holds AGENTBOX_AUTH_TOKEN; the shell must not.

    _child_env() builds the environment explicitly rather than inheriting,
    precisely so a terminal session cannot read the secret that let it in.
    """
    async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
        await _drain(ws, 2.0)
        await ws.send(b"echo TOKEN_IS[$AGENTBOX_AUTH_TOKEN]\n")
        output = await _drain(ws, 6.0)

        assert b"TOKEN_IS[]" in output, (
            f"the shell inherited the auth token: {output[-400:]!r}"
        )
        assert TOKEN.encode() not in output, output[-400:]


# ── cleanup ──────────────────────────────────────────────────────────

@requires_docker_introspection
async def test_shell_processes_do_not_leak_across_sessions(servers):
    """Same discipline as the Chromium leak checks in test_resilience.py."""
    def shell_count() -> int:
        out = subprocess.run(
            ["docker", "top", "agentbox-terminal-on", "-eo", "pid,comm"],
            capture_output=True, text=True, check=True,
        ).stdout
        return sum(1 for line in out.splitlines()
                   if any(sh in line for sh in ("bash", "zsh", "tmux")))

    before = shell_count()

    for _ in range(3):
        async with websockets.connect(_ws_url(servers["on"], TOKEN)) as ws:
            await _drain(ws, 1.0)
            await ws.send(b"echo cycle\n")
            await _drain(ws, 2.0)

    await asyncio.sleep(3)
    after = shell_count()

    # tmux keeps ONE server + session alive on purpose (new-session -A is
    # what makes a dropped socket reattach), so the count may rise once
    # and then hold. What must not happen is one more shell per session.
    assert after <= before + 2, (
        f"shell processes leaked across sessions: {before} -> {after}"
    )
