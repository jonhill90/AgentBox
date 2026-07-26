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
import re
import subprocess
import uuid
import time
import urllib.error
import urllib.request

import pytest

from conftest import CONTAINER, REPO_ROOT, requires_docker_introspection

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
    """Plain URL. Tokens no longer go here — see _connect()."""
    return f"ws://localhost:{port}/terminal"


def _connect(port: int, token: str | None = None):
    """Connect using the Authorization header, as a non-browser client should.

    RFC 9700 4.3.2 forbids tokens in query parameters, so the header path
    is what machine clients use; browsers (which cannot set headers) use
    the post-connect auth message exercised separately below.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return websockets.connect(_ws_url(port), additional_headers=headers)


_ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][B0]|\x1b[=><]")


def _plain(raw: bytes) -> bytes:
    """Terminal output with escape sequences removed.

    tmux redraws are full of cursor-positioning and mode escapes that contain
    digits and letters, so raw substring assertions match things that are not
    program output at all.
    """
    return _ANSI.sub(b"", raw)


def _fresh_shell(container: str = "agentbox-terminal-on") -> None:
    """Kill the tmux server so the next connect gets a clean session.

    terminal.py uses `tmux new-session -A`, which is what makes a dropped
    socket reattach — but it also means every test in this file shares one
    session and sees the previous test's scrollback on reattach. Without this
    the file is order-dependent and assertions match other tests' output.
    """
    subprocess.run(["docker", "exec", container, "tmux", "kill-server"],
                   capture_output=True, check=False)
    time.sleep(0.5)


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
        async with _connect(servers["off"], TOKEN):
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
        async with _connect(servers["no_token"], "anything"):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
async def test_fail_closed_even_with_no_token_supplied(servers):
    with pytest.raises(Exception):
        async with _connect(servers["no_token"]):
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
async def test_missing_credential_gets_a_challenge_not_a_shell(servers):
    """With no Authorization header the socket opens — that is the browser
    path — but it is challenged and owns nothing until it answers. The
    "no shell before auth" guarantee is asserted in
    test_no_pty_is_spawned_before_auth."""
    async with websockets.connect(_ws_url(servers["on"])) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert first == {"type": "auth_required"}, first


@requires_docker_introspection
async def test_wrong_token_is_refused(servers):
    with pytest.raises(Exception) as exc:
        async with _connect(servers["on"], WRONG_TOKEN):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
async def test_right_token_connects(servers):
    """Asserting on ws.state cannot fail once connect() returns, so this
    proves the socket is usable instead."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await ws.send(b"echo connect-$((3*4))-ok\n")
        out = _plain(await _drain(ws, 8.0))
        assert b"connect-12-ok" in out, out[-300:]


# ── the actual port: a real shell round-trip ─────────────────────────

@requires_docker_introspection
async def test_echo_round_trip_through_the_pty(servers):
    """The proof the relay works, equivalent to navigate-then-screenshot."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)  # let the shell/tmux draw its prompt

        # The marker is COMPUTED by the shell. zsh echoes typed characters
        # back over the PTY, so asserting on a literal that appears in the
        # command line passes whether or not the command ever ran — that is
        # how this test used to pass vacuously.
        await ws.send(b"echo agentbox-$((21*2))-works\n")
        output = await _drain(ws, 6.0)

        assert b"agentbox-42-works" in output, output[-400:]


@requires_docker_introspection
async def test_shell_sees_the_workspace(servers):
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)
        await ws.send(b"pwd\n")
        output = await _drain(ws, 6.0)
        assert b"/workspace" in output, output[-400:]


@requires_docker_introspection
async def test_resize_control_frame_reaches_the_shell(servers):
    """A JSON text frame must move the child's idea of the terminal size."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)

        # 137, not 100: tmux emits mouse-mode escapes like \x1b[?1000h on
        # redraw, so b"100" appeared in the drain no matter what the width
        # was. Escapes are also stripped before asserting, for the same
        # reason — the digits inside them are not program output.
        await ws.send(json.dumps({"type": "resize", "cols": 137, "rows": 30}))
        await asyncio.sleep(0.5)

        await ws.send(b"tput cols\n")
        output = _plain(await _drain(ws, 6.0))
        assert b"137" in output, output[-400:]


@requires_docker_introspection
async def test_unknown_control_frames_are_ignored(servers):
    """Hill90's client sends {"type":"ping"}; the server must tolerate it."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)

        await ws.send(json.dumps({"type": "ping"}))
        await ws.send("not json at all")
        await asyncio.sleep(0.3)

        # Still alive and still relaying.
        await ws.send(b"echo still-$((6*7))-here\n")
        output = await _drain(ws, 6.0)
        assert b"still-42-here" in output, output[-400:]


@requires_docker_introspection
async def test_the_shell_does_not_inherit_the_auth_token(servers):
    """The server process holds AGENTBOX_AUTH_TOKEN; the shell must not.

    _child_env() builds the environment explicitly rather than inheriting,
    precisely so a terminal session cannot read the secret that let it in.
    """
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
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
        async with _connect(servers["on"], TOKEN) as ws:
            await _drain(ws, 1.0)
            await ws.send(b"echo cycle\n")
            await _drain(ws, 2.0)

    await asyncio.sleep(3)
    after = shell_count()

    # tmux keeps ONE server + session alive on purpose (new-session -A is
    # what makes a dropped socket reattach), so the count may rise once
    # and then hold. What must not happen is one more shell per session.
    assert after <= before + 1, (
        f"shell processes leaked across sessions: {before} -> {after}"
    )


# ── privilege (SPEC §15.6) ───────────────────────────────────────────

@requires_docker_introspection
def test_server_process_does_not_run_as_root(servers):
    """The whole container dropped root, not just the shell."""
    out = subprocess.run(
        ["docker", "top", "agentbox-terminal-on", "-eo", "user,pid,comm"],
        capture_output=True, text=True, check=True,
    ).stdout
    server_lines = [ln for ln in out.splitlines()[1:] if "python" in ln]
    assert server_lines, out
    for line in server_lines:
        user = line.split()[0]
        assert user not in ("root", "0"), f"server is running as root: {line}"


@requires_docker_introspection
async def test_the_shell_is_not_root(servers):
    """A root PTY is a materially worse thing to hand out than an
    unprivileged one. This is the test that keeps it that way."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)
        await ws.send(b"id -u; id -un\n")
        output = await _drain(ws, 6.0)

        assert b"uid=0" not in output, f"the shell is root: {output[-300:]!r}"
        assert b"agentbox" in output, output[-300:]
        assert b"\r\n0\r\n" not in output, f"uid 0: {output[-300:]!r}"


@requires_docker_introspection
async def test_the_shell_can_write_to_the_workspace(servers):
    """Dropping root must not cost the shell its own workspace — the
    entrypoint chowns the volume precisely so this keeps working."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)
        marker = f"/workspace/.perm-{uuid.uuid4().hex[:8]}"
        await ws.send(("touch " + marker + "\n").encode())
        await _drain(ws, 5.0)
        await asyncio.sleep(1.0)

    # Checked from outside the terminal, so the shell's own echo cannot
    # satisfy it — the same technique test_no_pty_is_spawned_before_auth uses.
    check = subprocess.run(
        ["docker", "exec", "agentbox-terminal-on", "test", "-f", marker],
        capture_output=True,
    )
    assert check.returncode == 0, f"{marker} was not created — the shell cannot write"


@requires_docker_introspection
async def test_no_zsh_first_run_wizard(servers):
    """zsh runs zsh-newuser-install when ~/.zshrc is missing, which eats
    the opening keystrokes of a session. theme/zshrc exists to stop it."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        opening = await _drain(ws, 3.0)
        assert b"newuser" not in opening.lower(), opening[-400:]
        assert b"Aborting" not in opening, opening[-400:]

        # And the very first keystrokes are not swallowed.
        await ws.send(b"echo first-$((20+2))-survive\n")
        output = await _drain(ws, 6.0)
        assert b"first-22-survive" in output, output[-400:]


@requires_docker_introspection
async def test_the_shell_gets_the_tmux_theme(servers):
    """The Tokyo Night config has to be found in the new HOME."""
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 2.0)
        await ws.send(b"tmux show -g status-position; tmux show -gq @theme_variation\n")
        output = await _drain(ws, 6.0)
        assert b"top" in output, output[-300:]
        assert b"night" in output, output[-300:]


# ── credential must not travel in the URL (RFC 9700 §4.3.2) ──────────

@requires_docker_introspection
async def test_token_in_query_string_is_refused(servers):
    """Not merely discouraged — refused, so the old shape cannot linger.

    RFC 9700 (BCP 240) §4.3.2: "Clients MUST NOT pass access tokens in a
    URI query parameter." Honouring it would keep the violation alive for
    anyone still using it, and URLs reach access logs and history.
    """
    with pytest.raises(Exception) as exc:
        async with websockets.connect(f"{_ws_url(servers['on'])}?token={TOKEN}"):
            pass
    _assert_handshake_rejected(exc.value)


@requires_docker_introspection
def test_the_ui_never_puts_the_token_in_the_websocket_url(servers):
    src = (REPO_ROOT / "src" / "ui.html").read_text()
    assert "?token=" not in src, "UI still builds a token-bearing WebSocket URL"
    # The quoted-key form never appears — ui.html uses a JS object literal —
    # so an `or` on it was a dead alternative that made the check looser
    # than it read.
    assert 'type: "auth"' in src, "UI has no post-connect auth message"


# ── browser path: auth as the first message ─────────────────────────

@requires_docker_introspection
async def test_browser_style_post_connect_auth(servers):
    """Browsers cannot set headers, so the token goes in message one."""
    async with websockets.connect(_ws_url(servers["on"])) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert hello["type"] == "auth_required", hello

        await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        ok = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert ok["type"] == "auth_ok", ok

        await _drain(ws, 2.0)
        await ws.send(b"echo post-$((40+2))-auth-works\n")
        out = await _drain(ws, 6.0)
        assert b"post-42-auth-works" in out, out[-300:]


@requires_docker_introspection
async def test_post_connect_auth_rejects_a_wrong_token(servers):
    async with websockets.connect(_ws_url(servers["on"])) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps({"type": "auth", "token": WRONG_TOKEN}))
        with pytest.raises(Exception):
            await asyncio.wait_for(ws.recv(), timeout=10)
    assert ws.close_code == 4001, ws.close_code


@requires_docker_introspection
async def test_no_pty_is_spawned_before_auth(servers):
    """The ttyd pre-auth RCE (NCC Group 2017) was exactly this: the
    handshake was authenticated but the receive path was not. Sending
    shell input before authenticating must reach no shell."""
    async with websockets.connect(_ws_url(servers["on"])) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)   # auth_required
        await ws.send(b"echo SHOULD-NEVER-RUN > /workspace/preauth.txt\n")
        await asyncio.sleep(1.0)

    check = subprocess.run(
        ["docker", "exec", "agentbox-terminal-on", "test", "-f", "/workspace/preauth.txt"],
        capture_output=True,
    )
    assert check.returncode != 0, "pre-auth input reached a shell"


@requires_docker_introspection
async def test_unauthenticated_socket_times_out(servers):
    """The pre-auth window must not be held open indefinitely."""
    async with websockets.connect(_ws_url(servers["on"])) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)   # auth_required
        with pytest.raises(Exception):
            # AUTH_TIMEOUT is 10s server-side; wait past it.
            await asyncio.wait_for(ws.recv(), timeout=20)
    assert ws.close_code == 4001, ws.close_code


# ── prompt (Powerlevel10k) ───────────────────────────────────────────

@requires_docker_introspection
async def test_prompt_is_powerlevel10k_with_os_and_git_segments(servers):
    """The prompt must be the operator's real p10k, not an approximation.

    hill90-app dropped the `vcs` segment because gitstatusd was not
    bundled, which loses the git branch and status icons. The daemon is
    pre-fetched at build time here so the segment works.
    """
    _fresh_shell()
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 3.0)
        await ws.send(b"print -l -- $POWERLEVEL9K_LEFT_PROMPT_ELEMENTS\n")
        out = (await _drain(ws, 6.0)).decode(errors="replace")

        for segment in ("os_icon", "dir", "vcs", "prompt_char"):
            assert segment in out, f"{segment} missing from the prompt: {out[-300:]}"


@requires_docker_introspection
def test_gitstatusd_is_present_for_the_vcs_segment(servers):
    """Without the daemon p10k would try to download it at first prompt,
    at runtime, with no network."""
    listing = subprocess.run(
        ["docker", "exec", "agentbox-terminal-on", "sh", "-c",
         "ls /home/agentbox/.cache/gitstatus/"],
        capture_output=True, text=True,
    )
    assert "gitstatusd" in listing.stdout, listing.stdout + listing.stderr


@requires_docker_introspection
async def test_the_vcs_segment_reports_a_real_repository(servers):
    """Make a repo with an untracked file and assert the prompt says so."""
    _fresh_shell()
    subprocess.run(
        ["docker", "exec", "-u", "agentbox", "agentbox-terminal-on", "sh", "-c",
         "cd /workspace && git init -q 2>/dev/null; "
         "git config user.email t@t.t; git config user.name t; "
         "echo x > vcs-probe.txt && git add vcs-probe.txt && git commit -qm probe; "
         "echo y > vcs-untracked.txt"],
        capture_output=True, check=False,
    )
    # A branch name that cannot appear anywhere else — not in the prompt's
    # dir segment, not in the echoed command. The old assertion looked for
    # "workspace", which is in both, so it passed without the vcs segment
    # rendering at all.
    branch = f"probe-{uuid.uuid4().hex[:6]}"
    subprocess.run(
        ["docker", "exec", "-u", "agentbox", "agentbox-terminal-on", "sh", "-c",
         f"cd /workspace && git checkout -q -b {branch}"],
        capture_output=True, check=False,
    )
    async with _connect(servers["on"], TOKEN) as ws:
        await _drain(ws, 4.0)
        # Force a fresh prompt so gitstatus repaints the vcs segment.
        await ws.send(b"cd /workspace && true\n")
        out = (await _drain(ws, 10.0)).decode(errors="replace")
        assert branch in out, f"vcs segment never rendered the branch: {out[-400:]}"

    branch = subprocess.run(
        ["docker", "exec", "-u", "agentbox", "agentbox-terminal-on", "sh", "-c",
         "cd /workspace && git rev-parse --abbrev-ref HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch, "no branch — the repo probe did not take"
