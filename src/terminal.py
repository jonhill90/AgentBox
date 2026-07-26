#!/usr/bin/env python3
"""
WebSocket terminal — bidirectional PTY relay (PRD 1.11 / SPEC §15).

Ported from hill90-app's `services/agentbox/app/ws_terminal.py`
(read-only reference). Spawns a PTY per connection and relays stdin and
stdout between the WebSocket and the PTY master fd.

Wire format (unchanged from Hill90):
  - Binary frames: raw terminal I/O (stdin from client, stdout to client)
  - Text frames:   JSON control messages
      {"type": "resize", "cols": 120, "rows": 40}

Hill90's client also sends {"type": "ping"} keep-alives that the server
does not understand; they fall into the control handler's except and are
ignored. That tolerance is ported deliberately rather than replaced with
an opinion.

Gated twice over, and both gates are real:

  - `AGENTBOX_ENABLE_TERMINAL` must be on, or the route is never
    registered and there is nothing to connect to (SPEC §14 item 4).
  - `AGENTBOX_AUTH_TOKEN` must be set and must be presented, or no PTY
    is ever spawned. FAIL CLOSED: with no token configured the terminal
    refuses every connection, even though the rest of the server treats
    "no token" as "auth off". Hill90 does the same and never exposes
    this socket unauthenticated even to itself (SPEC §14 item 6).

TWO WAYS TO AUTHENTICATE, and deliberately NOT via `?token=`:

  1. `Authorization: Bearer <token>` on the handshake. Non-browser
     clients can set headers, so they should — this is checked BEFORE
     accept(), so an unauthorised machine client never gets a socket.
  2. Auth as the first message, for browsers. The browser WebSocket API
     cannot set request headers (MDN: only the `protocols` argument is
     client-controlled), so the socket is accepted, the server sends
     {"type":"auth_required"}, and the client must reply
     {"type":"auth","token":"..."} within AUTH_TIMEOUT seconds.

Hill90 passed the token as `?token=`, and this port deliberately does
not. RFC 9700 (BCP 240, Jan 2025) §4.3.2 hardens RFC 6750's SHOULD NOT
into "Clients MUST NOT pass access tokens in a URI query parameter" —
URLs land in access logs, proxy traces and browser history. Since the
same secret guards the HTTP surface, one logged URL would be total
compromise of an endpoint that grants a shell.

The pre-auth window is the dangerous part, and it is why the PTY is
spawned only AFTER auth succeeds. ttyd <=1.3.0 authenticated its
handshake but not its WebSocket receive path, which was a pre-auth RCE
(NCC Group advisory, 2017-09-08). Before auth this handler accepts
exactly one message type, caps its size, and holds a deadline.

The shell is NOT root. The container runs as the `agentbox` user (uid
1000) — docker-entrypoint.sh fixes volume ownership and then drops
privileges with setpriv before the server ever starts — so the PTY
hands out an unprivileged shell, matching Hill90's `agentuser`.
tests/test_terminal.py asserts this rather than trusting it.
"""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import os
import pty
import select
import shutil
import signal
import struct
import termios

from starlette.websockets import WebSocket, WebSocketDisconnect

import auth

logger = logging.getLogger(__name__)

TERM_COLS = 120
TERM_ROWS = 40
READ_SIZE = 4096
TMUX_SESSION = "agent"

# Close code for an auth failure, kept identical to Hill90's.
#
# Header-authenticated clients are rejected BEFORE accept(), so uvicorn
# never completes the handshake and they see an HTTP 403 instead of this
# code. Clients that reach the post-connect auth exchange DO see 4001,
# because by then a socket exists to close. Both mean "do not retry".
WS_UNAUTHORIZED = 4001

# How long a connected-but-unauthenticated socket may live. Short on
# purpose: this is the only window in which an unauthenticated peer holds
# a socket at all.
AUTH_TIMEOUT = 10.0

# Rejected above this size. Note the frame is fully buffered by the
# websockets layer before we see it, so the real memory bound is that
# library's own max-size, not this number.
MAX_AUTH_MESSAGE_BYTES = 4096

# How many non-auth control frames to tolerate before giving up.
MAX_AUTH_ATTEMPTS = 5

# PTY readers park a thread in a blocking select() for the life of a session.
# They get their OWN pool: parked on asyncio's default executor they would
# occupy the same threads asyncio.to_thread uses for every browser tool and
# /api/browser/* route, so enough terminal sessions would wedge the entire
# HTTP surface with no error.
# Unauthenticated sockets are cheap to open and live for AUTH_TIMEOUT, so
# without a ceiling they are a free pre-auth resource sink. Authenticated
# sessions are not affected — this counter is released as soon as auth
# succeeds or fails.
MAX_PENDING_AUTH = 16
_pending_auth = 0

_PTY_READERS = concurrent.futures.ThreadPoolExecutor(
    max_workers=64, thread_name_prefix="agentbox-pty-reader",
)


def _resolve_shell() -> tuple[list[str], str]:
    """Resolve the best available shell command.

    Prefers tmux (so a dropped socket reattaches rather than losing the
    session) then zsh, then bash. Returns (argv, binary) for execvpe.
    """
    tmux = shutil.which("tmux")
    zsh = shutil.which("zsh")
    bash = shutil.which("bash") or "/bin/bash"

    if tmux and zsh:
        return (
            [tmux, "new-session", "-A", "-s", TMUX_SESSION,
             "-x", str(TERM_COLS), "-y", str(TERM_ROWS)],
            tmux,
        )
    if tmux:
        # Hill90 requires both; here tmux alone is still worth having for
        # the reattach behaviour, with whatever shell tmux defaults to.
        return (
            [tmux, "new-session", "-A", "-s", TMUX_SESSION,
             "-x", str(TERM_COLS), "-y", str(TERM_ROWS)],
            tmux,
        )
    if zsh:
        return ([zsh, "--login"], zsh)
    return ([bash, "--login"], bash)


def _child_env() -> dict[str, str]:
    """The environment the shell gets — built explicitly, never inherited.

    Defence in depth only. The shell runs as the SAME uid as this process, so
    it could read /proc/1/environ regardless — what actually keeps the token
    out of its reach is auth.py scrubbing the values from os.environ once
    they are loaded.
    """
    home = os.environ.get("HOME", "/root")
    user = os.environ.get("USER", "root")
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "USER": user,
        "LOGNAME": user,
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "SHELL": shutil.which("zsh") or shutil.which("bash") or "/bin/bash",
        "AGENTBOX_WORKSPACE": "/workspace",
    }


async def _authenticate(websocket: WebSocket) -> bool:
    """Authenticate the socket. Returns True only if a PTY may be spawned.

    Path 1 — Authorization header, checked before accept(). Path 2 — the
    post-connect auth exchange, for browsers that cannot set headers.
    """
    # Path 1: header. Machine clients should use this.
    header = websocket.headers.get("authorization")
    if header:
        if auth.check_bearer(header):
            await websocket.accept()
            logger.info("Terminal authenticated via Authorization header")
            return True
        logger.warning("Terminal rejected: bad Authorization header")
        await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
        return False

    # A token in the query string is refused outright rather than
    # honoured — accepting it would keep the RFC 9700 violation alive for
    # anyone who kept using the old shape.
    if websocket.query_params.get("token"):
        logger.warning(
            "Terminal rejected: token in query string. Send an Authorization "
            "header, or authenticate with the first message (RFC 9700 4.3.2)."
        )
        await websocket.close(code=WS_UNAUTHORIZED, reason="token in query string")
        return False

    if not auth.auth_enabled():
        # Fail closed: no configured token means no terminal, ever.
        logger.warning("Terminal rejected: no AGENTBOX_AUTH_TOKEN configured")
        await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
        return False

    # Path 2: post-connect auth. The socket exists but owns nothing yet.
    global _pending_auth
    if _pending_auth >= MAX_PENDING_AUTH:
        logger.warning("Terminal rejected: too many unauthenticated sockets pending")
        await websocket.close(code=WS_UNAUTHORIZED, reason="busy")
        return False
    _pending_auth += 1
    try:
        return await _post_connect_auth(websocket)
    finally:
        _pending_auth -= 1


async def _post_connect_auth(websocket: WebSocket) -> bool:
    """The browser path: accept, challenge, and require an auth message."""
    await websocket.accept()
    deadline = asyncio.get_event_loop().time() + AUTH_TIMEOUT
    try:
        await websocket.send_text(json.dumps({"type": "auth_required"}))

        # A client may emit an unrelated control frame (a resize on open,
        # a keep-alive ping) before it answers the challenge. Those are
        # ignored rather than fatal — but BINARY frames are shell input,
        # and accepting those before auth is precisely the ttyd pre-auth
        # RCE, so they end the connection. The deadline and the message
        # cap bound this window either way.
        for _ in range(MAX_AUTH_ATTEMPTS):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            message = await asyncio.wait_for(websocket.receive(), timeout=remaining)

            if message.get("type") == "websocket.disconnect":
                return False
            if message.get("bytes"):
                logger.warning("Terminal rejected: input sent before authentication")
                await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
                return False

            raw = message.get("text") or ""
            if not raw or len(raw) > MAX_AUTH_MESSAGE_BYTES:
                logger.warning("Terminal rejected: malformed pre-auth message")
                await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
                return False

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Terminal rejected: non-JSON pre-auth message")
                await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
                return False

            if payload.get("type") != "auth":
                continue  # unrelated control frame; not yet authenticated

            if not auth.check_token(payload.get("token")):
                logger.warning("Terminal rejected: bad auth token")
                await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
                return False
            break
        else:
            logger.warning("Terminal rejected: no auth message in time")
            await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
            return False

    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Terminal rejected: auth timed out")
        await websocket.close(code=WS_UNAUTHORIZED, reason="auth timeout")
        return False
    except (WebSocketDisconnect, RuntimeError):
        return False

    await websocket.send_text(json.dumps({"type": "auth_ok"}))
    logger.info("Terminal authenticated via post-connect auth message")
    return True


async def terminal_websocket(websocket: WebSocket) -> None:
    """Handle a WebSocket terminal session."""
    if not await _authenticate(websocket):
        return

    logger.info("Terminal session opened")

    master_fd = -1
    pid = -1

    try:
        master_fd, slave_fd = pty.openpty()

        winsize = struct.pack("HHHH", TERM_ROWS, TERM_COLS, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        pid = os.fork()

        if pid == 0:
            # Child — become session leader, wire the slave to std fds, exec.
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            os.close(master_fd)
            os.close(slave_fd)

            try:
                os.chdir("/workspace")
            except OSError:
                try:
                    os.chdir(os.path.expanduser("~"))
                except OSError:
                    pass

            argv, shell_bin = _resolve_shell()
            try:
                os.execvpe(shell_bin, argv, _child_env())
            except OSError as exc:
                os.write(2, f"exec failed: {exc}\n".encode())
                os._exit(127)

        # Parent.
        os.close(slave_fd)

        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        reader_task = asyncio.create_task(_pty_reader(master_fd, websocket))

        try:
            while True:
                message = await websocket.receive()

                if message.get("type") == "websocket.disconnect":
                    break

                if message.get("bytes"):
                    # The fd is O_NONBLOCK, so a single write can be partial
                    # and the unwritten tail would be silently lost — visible
                    # as dropped characters on a large paste.
                    data = message["bytes"]
                    while data:
                        try:
                            data = data[os.write(master_fd, data):]
                        except BlockingIOError:
                            await asyncio.sleep(0.01)

                elif message.get("text"):
                    try:
                        ctrl = json.loads(message["text"])
                        if ctrl.get("type") == "resize":
                            cols = int(ctrl.get("cols", TERM_COLS))
                            rows = int(ctrl.get("rows", TERM_ROWS))
                            winsize = struct.pack("HHHH", rows, cols, 0, 0)
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                            os.kill(pid, signal.SIGWINCH)
                    except (json.JSONDecodeError, ValueError, OSError):
                        pass  # unknown control frames (e.g. ping) are ignored

        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

    except Exception as exc:
        logger.error("Terminal WebSocket error: %s", exc, exc_info=True)
    finally:
        if master_fd >= 0:
            try:
                os.close(master_fd)
            except OSError:
                pass

        if pid > 0:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            # Actually reap it. A bare WNOHANG here returns before the child
            # has died and leaves a zombie per session — with PidsLimit=200,
            # ~180 connect/disconnect cycles exhausted the PID namespace and
            # nothing could fork again for the life of the container.
            for _ in range(50):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] != 0:
                        break
                except ChildProcessError:
                    break
                await asyncio.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (OSError, ChildProcessError):
                    pass
        logger.info("Terminal session closed")


async def _pty_reader(master_fd: int, websocket: WebSocket) -> None:
    """Read from the PTY master and forward to the WebSocket as binary."""
    loop = asyncio.get_event_loop()

    try:
        while True:
            ready = await loop.run_in_executor(
                _PTY_READERS, lambda: select.select([master_fd], [], [], 0.1)
            )

            if ready[0]:
                try:
                    data = os.read(master_fd, READ_SIZE)
                    if not data:
                        break
                    await websocket.send_bytes(data)
                except OSError:
                    break
            else:
                await asyncio.sleep(0)

    except (asyncio.CancelledError, WebSocketDisconnect):
        pass
    except Exception as exc:
        logger.debug("PTY reader stopped: %s", exc)
