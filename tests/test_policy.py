#!/usr/bin/env python3
"""
The allowlist trip-wire (SPEC.md §7, last bullet).

Both lists are empty as of this commit and nothing reads them. These
assertions are trivial on purpose: they exist so that granting the box a
new capability shows up in review as an intentional, explained diff —
including a change to this file — rather than as a quiet line in a
config nobody looks at.

If you are here because this test failed: that is the mechanism working.
Confirm the addition is deliberate, scoped, and reviewed (PRD 1.5), then
update the expectation.

Unlike the rest of the suite this needs no container — it imports the
module directly.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import policy  # noqa: E402  (path setup must precede the import)


def test_command_allowlist_is_empty():
    assert policy.COMMAND_ALLOWLIST == [], (
        "COMMAND_ALLOWLIST gained an entry. Phase 1 has no execute_command "
        "tool and no policy-checked call site, so an entry here grants "
        "nothing and signals scope creep — see PRD 1.2 and SPEC §9 item 4."
    )


def test_network_allowlist_is_empty():
    assert policy.NETWORK_ALLOWLIST == [], (
        "NETWORK_ALLOWLIST gained an entry. Nothing enforces this list yet "
        "(SPEC §7 defers real network enforcement to Phase 2), so an entry "
        "here is a declaration of intent that no boundary is honouring."
    )


def test_nothing_consults_the_allowlists():
    """The allowlist scaffolding must stay unwired until a real need is scoped.

    `PathPolicy` in the same module *is* used (it scopes the filesystem
    tools) — this checks the two lists specifically, not the module.
    """
    for source in sorted(SRC.glob("*.py")):
        if source.name == "policy.py":
            continue
        # Comments may discuss the allowlists (jumpbox_tools.py documents
        # where a future exception would be checked); executable code may
        # not consult them.
        code = "\n".join(
            line for line in source.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#")
        )
        for name in ("COMMAND_ALLOWLIST", "NETWORK_ALLOWLIST"):
            assert name not in code, (
                f"{source.name} references {name!r} in code: the allowlists are meant "
                "to be unused in this phase. Wiring them up is a separate decision."
            )


# ── PathPolicy (SPEC §8) — the live half of this module ──────────────

def test_path_policy_allows_inside_the_workspace():
    p = policy.PathPolicy(allowed_paths=["/workspace"])
    for path in ("/workspace", "/workspace/a.txt", "/workspace/deep/nested/file"):
        allowed, reason = p.check_read(path)
        assert allowed, f"{path}: {reason}"


def test_path_policy_denies_outside_the_workspace():
    p = policy.PathPolicy(allowed_paths=["/workspace"])
    for path in ("/etc/passwd", "/", "/app/src/mcp_server.py", "/root/.ssh/id_rsa"):
        allowed, reason = p.check_read(path)
        assert not allowed, f"{path} was allowed"
        assert "not in allowed paths" in reason


def test_path_policy_resolves_traversal():
    p = policy.PathPolicy(allowed_paths=["/workspace"])
    allowed, _ = p.check_read("/workspace/../etc/passwd")
    assert not allowed, "traversal escaped the workspace"


def test_path_policy_does_not_treat_a_prefix_as_a_child():
    """/workspaceless must not pass as a child of /workspace."""
    p = policy.PathPolicy(allowed_paths=["/workspace"])
    allowed, _ = p.check_read("/workspaceless/secret")
    assert not allowed


def test_path_policy_denied_list_wins_over_allowed():
    p = policy.PathPolicy(allowed_paths=["/workspace"], denied_paths=["/workspace/secret"])
    assert p.check_read("/workspace/ok.txt")[0]
    allowed, reason = p.check_read("/workspace/secret/keys.txt")
    assert not allowed and "denied list" in reason


def test_path_policy_read_only_blocks_writes_but_not_reads():
    p = policy.PathPolicy(allowed_paths=["/workspace"], read_only=True)
    assert p.check_read("/workspace/a.txt")[0]
    allowed, reason = p.check_write("/workspace/a.txt")
    assert not allowed and reason == "Filesystem is read-only"


def test_path_policy_defaults_to_the_workspace_root():
    p = policy.PathPolicy()
    assert p.allowed == [policy.WORKSPACE_ROOT]
    assert p.check_read("/etc/passwd")[0] is False


def test_filesystem_tools_are_scoped_to_the_workspace_only():
    """The shipped policy, not a hand-built one: one root, not read-only."""
    import jumpbox_tools

    assert jumpbox_tools._policy.allowed == [policy.WORKSPACE_ROOT], (
        "the filesystem tools' path policy no longer points at just the workspace"
    )
    assert jumpbox_tools._policy.read_only is False
