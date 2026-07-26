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
  - `AGENTBOX_AUTH_TOKEN` must be set and must match the `?token=` query
    param, or the socket is closed with 4001 before it is ever accepted.
    Note this is FAIL CLOSED: with no token configured the terminal
    refuses every connection, even though the rest of the server treats
    "no token" as "auth off". Hill90 does the same and never exposes
    this socket unauthenticated even to itself (SPEC §14 item 6).

KNOWN LIMITATION: the container currently runs as root, so this is a
root shell. Hill90's equivalent drops to a dedicated `agentuser`. Fixing
that touches /workspace ownership, the Playwright browser cache and the
screenshots volume, so it is deliberately a separate change — see
SPEC §15.6. Until then the two gates above are what stands between the
port and a root PTY.
"""

import asyncio
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
# OBSERVABLE BEHAVIOUR, verified: because the close happens BEFORE
# accept(), uvicorn never completes the handshake and the client sees an
# HTTP 403 rejection — close code 4001 never reaches it. (Hill90 has the
# same property, which makes its UI's `event.code === 4001` guard dead
# code there.) Rejecting pre-accept is the stronger posture, so the code
# stays and the clients treat a 403 handshake rejection as "do not
# retry" alongside 4001.
WS_UNAUTHORIZED = 4001


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

    The server process holds AGENTBOX_AUTH_TOKEN; handing its environment
    to a shell would leak the token to anything run in the terminal.
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


async def terminal_websocket(websocket: WebSocket) -> None:
    """Handle a WebSocket terminal session."""
    # Auth BEFORE accept, so an unauthorised client never gets a live
    # socket. check_token is fail-closed when no token is configured.
    token = websocket.query_params.get("token", "")
    if not auth.check_token(token):
        logger.warning("Unauthorized terminal connection attempt")
        await websocket.close(code=WS_UNAUTHORIZED, reason="unauthorized")
        return

    await websocket.accept()
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
                    os.write(master_fd, message["bytes"])

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
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        logger.info("Terminal session closed")


async def _pty_reader(master_fd: int, websocket: WebSocket) -> None:
    """Read from the PTY master and forward to the WebSocket as binary."""
    loop = asyncio.get_event_loop()

    try:
        while True:
            ready = await loop.run_in_executor(
                None, lambda: select.select([master_fd], [], [], 0.1)
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
