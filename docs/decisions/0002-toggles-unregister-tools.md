# 0002 — Disabled tools are absent, not refusing

**Status:** accepted.

## Decision

Feature toggles are evaluated at **registration time**. When
`AGENTBOX_ENABLE_JUMPBOX_TOOLS` is false the filesystem, git and
`http_request` tools are never handed to FastMCP, so they do not appear in
`list_tools()` at all.

## Rejected: register, then refuse

The usual shape — register every tool and return "disabled" when called —
leaves the tool visible in discovery. An agent then spends turns trying a
capability that cannot work, and a reader of `list_tools()` gets a false
picture of the box's reach.

## Consequences

- `list_tools()` is an honest inventory of what this instance can do.
- The toggle must be read before registration, so it cannot be changed
  without a restart. Accepted: these are deployment-shape decisions.
- Tests assert **absence** from `list_tools()`, not a refusing response —
  see `tests/test_feature_toggle.py`.

**Detail:** [`../architecture/overview.md`](../architecture/overview.md)
§ Registration and toggles.
