#!/bin/sh
# AgentBox entrypoint — fix volume ownership, then drop root.
#
# Why an entrypoint rather than a bare `USER` directive: /workspace and
# the screenshots directory are named volumes. Docker seeds a *fresh*
# volume with the image's ownership, but a volume created by an earlier
# (root-running) build stays root-owned, and a plain USER switch would
# leave the server unable to write to its own workspace. Running this as
# root first makes the upgrade seamless in both directions.
set -e

APP_USER="${AGENTBOX_USER:-agentbox}"

fix_owner() {
    dir="$1"
    [ -d "$dir" ] || mkdir -p "$dir"
    # Recursive chown only when the top-level owner is actually wrong —
    # a workspace with thousands of files should not add seconds to every
    # container start.
    if [ "$(stat -c %U "$dir" 2>/dev/null)" != "$APP_USER" ]; then
        echo "agentbox-entrypoint: taking ownership of $dir for $APP_USER"
        chown -R "$APP_USER:$APP_USER" "$dir" 2>/dev/null || true
    fi
}

if [ "$(id -u)" = "0" ]; then
    fix_owner /workspace
    fix_owner /workspace/screenshots
    fix_owner /data/browsers

    # --inh-caps alone is NOT the escalation route. What matters for a PTY
    # is --no-new-privs (so a setuid-root binary cannot raise privilege at
    # exec; the image ships su, mount, passwd and friends) and clearing the
    # BOUNDING set, which caps what any descendant can ever hold.
    exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
         --inh-caps=-all --bounding-set=-all --no-new-privs "$@"
fi

# Already unprivileged (e.g. `docker run --user`); just run.
exec "$@"
