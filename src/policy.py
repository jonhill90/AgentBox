#!/usr/bin/env python3
"""
Policy: allowlist scaffolding (PRD 1.5 / SPEC §7) and path scoping (SPEC §8).

Two different mechanisms live here.

**The allowlists grant nothing.** Both lists below are empty, and
**nothing in this codebase reads them**: there is no `execute_command`
tool, no shell tool, no route and no MCP tool that consults
`COMMAND_ALLOWLIST`. They exist so that if a specific, narrow capability
is ever needed (reading one log source; reaching one internal host once
there is a deployment), granting it is a reviewed one-line data change
against an existing extension point — not a redesign improvised under
time pressure.

**`PathPolicy` is live.** It is a different mechanism — path scoping
rather than an allowlist of specific commands — and it is what confines
the filesystem tools (SPEC §8) to the workspace directory. It is ported
from Hill90's `services/agentbox/app/policy.py`.

Two rules this module encodes, both from PRD 1.5:

1. **The allowlist is data, not code.** Capabilities are added by
   appending an entry here, where the diff is small, obvious in review,
   and trivially revertible. They are not added by writing new
   conditional logic at a call site.

2. **Network enforcement must not rely on application-level checks
   alone.** An in-process check can contain a structured HTTP tool; it
   cannot contain a real shell, which can always open its own socket.
   `NETWORK_ALLOWLIST` is therefore a declaration of intent that a real
   network boundary is expected to enforce — see "Deferred" below.

Why the box is shaped this way: its eventual purpose (Phase 2) is a
jumpbox an agent uses because it has no other route to a network Claude
Code Web cannot otherwise reach. A full interactive shell or PTY is
explicitly rejected for the agent-facing case. Binary allowlisting does
not contain a real shell — once one exists, allowed binaries can be
chained — and an agent that also reads untrusted content from the open
web (its own browser tool) plus any shell access is the standard
prompt-injection-to-RCE path. Nothing here builds that, and nothing here
should be treated as a step toward it.

Deferred to Phase 2 (SPEC §13 item 3): the actual Docker-level
firewall/iptables enforcement of `NETWORK_ALLOWLIST`. There is nothing
to restrict until the list has an entry *and* there is a real deployment
with a network boundary to enforce it against. Wiring firewall rules now
would be speculative scaffolding against an environment that does not
exist yet.
"""

import os

# The single directory the filesystem and git tools may touch (SPEC §8).
# Not "/", and not the container's own source tree.
WORKSPACE_ROOT = "/workspace"

# Commands this container is ever permitted to execute.
#
# EMPTY, AND UNUSED. There is no caller. Phase 1 is browser-only.
#
# Each future entry is a dict of the shape:
#
#     {"binary": "<resolved-or-resolvable-path>", "args": [...]}
#
# ...to be enforced the way Hill90's `CommandPolicy.check()` does:
#
#   - resolve the binary to a real absolute path (`shutil.which` for a
#     bare name, then `os.path.realpath`) and compare *resolved paths*,
#     so a same-named binary earlier on PATH cannot impersonate an
#     allowed one;
#   - reject anything not on the list, rather than denylisting known-bad
#     input;
#   - `shell=False` always — never the unrestricted `shell=True` this
#     repo's first commit shipped, which made the allowlist meaningless
#     because the shell re-expands the string;
#   - scrub the environment passed to the child process.
#
# Adding an entry here is not sufficient on its own: it also requires a
# policy-checked call site, which is a deliberate, separately reviewed
# decision (PRD 1.2, SPEC §13 item 4). Adding an entry without one
# changes nothing.
COMMAND_ALLOWLIST: list[dict] = []

# Specific hosts this container may reach, *in addition to* the current
# baseline described below.
#
# EMPTY, AND NOT ENFORCED ANYWHERE YET.
#
# Current baseline, stated explicitly so it is not confused with this
# list: the container has ordinary outbound network access, because the
# browser tool's whole job is navigating to whatever URL it is given on
# the open internet. That baseline belongs to the browser tool alone.
#
# Do not read that baseline as "egress is unrestricted, so this list is
# pointless." The two are different things:
#
#   - "the browser may navigate anywhere" is a property of a structured
#     tool whose only verb is `page.goto`, whose results come back as
#     screenshots and text, and which cannot open an arbitrary socket;
#   - "tool/shell egress is unrestricted" is the thing this list exists
#     to prevent ever becoming true by accident.
#
# A future entry (e.g. an internal host reachable only once this is
# deployed) is a one-line reviewed addition here, plus the Phase 2
# network-layer enforcement noted in the module docstring.
NETWORK_ALLOWLIST: list[str] = []


class PathPolicy:
    """Validates file paths against allowed/denied lists with symlink resolution.

    Ported from Hill90's `services/agentbox/app/policy.py`.

    Every check resolves the path with `os.path.realpath` first, so a
    symlink inside the workspace pointing at `/etc` is rejected on the
    resolved target rather than the pretty name. Prefix comparisons are
    done against `allowed + "/"`, so `/workspaceless` does not pass as a
    child of `/workspace`.

    This is the only thing standing between the filesystem tools and the
    rest of the container's filesystem — it is deliberately a default-deny
    check (a path must match an allowed root) rather than a denylist.
    """

    def __init__(
        self,
        allowed_paths: list[str] | None = None,
        denied_paths: list[str] | None = None,
        read_only: bool = False,
    ):
        self.allowed = [os.path.realpath(p) for p in (allowed_paths or [WORKSPACE_ROOT])]
        self.denied = [os.path.realpath(p) for p in (denied_paths or [])]
        self.read_only = read_only

    def check_read(self, path: str) -> tuple[bool, str]:
        """Check if a path is allowed for reading. Returns (allowed, reason)."""
        real = os.path.realpath(path)

        for denied in self.denied:
            if real == denied or real.startswith(denied + "/"):
                return False, f"Path '{path}' is in denied list"

        for allowed in self.allowed:
            if real == allowed or real.startswith(allowed + "/"):
                return True, "ok"

        return False, f"Path '{path}' is not in allowed paths"

    def check_write(self, path: str) -> tuple[bool, str]:
        """Check if a path is allowed for writing. Returns (allowed, reason)."""
        if self.read_only:
            return False, "Filesystem is read-only"
        return self.check_read(path)
