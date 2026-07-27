#!/bin/sh
# Read-only git credential helper (SPEC §16).
#
# Answers `get` from AGENTBOX_GIT_CREDENTIALS_FILE and ignores `store` and
# `erase`. Git's built-in `store` helper implements all three, so anything in
# the container could rewrite or delete the operator's secret through git.
# This one cannot: it never opens the file for writing.
#
# The file is git's credential-store format, one entry per line:
#   https://<user>:<token>@github.com
set -e

[ "$1" = "get" ] || exit 0
[ -n "$AGENTBOX_GIT_CREDENTIALS_FILE" ] || exit 0
[ -r "$AGENTBOX_GIT_CREDENTIALS_FILE" ] || exit 0

# git sends the request as key=value lines on stdin.
proto=""; host=""
while IFS= read -r line; do
    case "$line" in
        protocol=*) proto=${line#protocol=} ;;
        host=*)     host=${line#host=} ;;
        "")         break ;;
    esac
done

while IFS= read -r entry || [ -n "$entry" ]; do
    case "$entry" in
        "$proto"://*"@$host") ;;
        *) continue ;;
    esac
    creds=${entry#*://}
    userpass=${creds%@*}
    printf 'username=%s\n' "${userpass%%:*}"
    printf 'password=%s\n' "${userpass#*:}"
    exit 0
done < "$AGENTBOX_GIT_CREDENTIALS_FILE"
