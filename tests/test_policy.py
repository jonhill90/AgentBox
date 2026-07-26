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


def test_nothing_in_the_server_consults_the_allowlists():
    """The scaffolding must stay unwired until a real need is scoped."""
    server_src = (SRC / "mcp_server.py").read_text(encoding="utf-8")
    for name in ("COMMAND_ALLOWLIST", "NETWORK_ALLOWLIST", "import policy", "from policy"):
        assert name not in server_src, (
            f"mcp_server.py references {name!r}: the allowlists are meant to be "
            "unused in Phase 1. Wiring them up is a separate, deliberate decision."
        )
