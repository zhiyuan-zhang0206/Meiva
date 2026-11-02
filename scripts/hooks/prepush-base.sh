#!/usr/bin/env bash
# One contribution range for every push selector, independent of old remote tips.
set -euo pipefail

# meiva:note
# This ref is the range every push selector measures against, and on a checkout that shares its
# repository with other work nothing keeps it fresh: on 2026-11-02 it had not moved for 2.6
# hours, so `merge-base(origin/main, HEAD)..HEAD` included three commits already merged into
# main. Their hooks then re-ran on this branch and failed -- a push blocked by someone else's
# code. Refreshing it here is best-effort and bounded, because needing the network to push
# would be worse than a stale range.
REFRESH_LIMIT_S="${MEIVA_PREPUSH_FETCH_TIMEOUT:-10}"
# meiva:note
# `alarm 0` cancels the alarm instead of arming it, and a non-number would make perl die before
# the fetch ever runs, so an unusable value falls back to the default rather than silently
# turning the bound off.
case "$REFRESH_LIMIT_S" in
'' | *[!0-9]* | 0) REFRESH_LIMIT_S=10 ;;
esac

# meiva:note
# `--no-prune` is the whole repair for a ref that vanishes: this machine sets
# `fetch.prune=true` globally, and a narrow refspec fetch under prune deletes the
# remote-tracking refs it did not just fetch. Three identical fetches here left `origin/main`
# present, missing, present -- and with it missing every push fails.
#
# `perl -e 'alarm shift; exec @ARGV'` is the portable bound: this platform has no GNU
# `timeout`, and the alarm survives the exec into git.
refresh_origin_main() {
    if command -v perl >/dev/null 2>&1; then
        perl -e 'alarm shift; exec @ARGV' "$REFRESH_LIMIT_S" \
            git fetch --quiet --no-prune origin "main:refs/remotes/origin/main" >/dev/null 2>&1
    else
        git fetch --quiet --no-prune origin "main:refs/remotes/origin/main" >/dev/null 2>&1
    fi
}

# meiva:note
# The message names the consequence, not just the failure: the range silently falls back to
# the local ref, so a reader has to know what that costs to decide whether to re-run the push.
if git rev-parse --verify -q origin/main >/dev/null; then
    refresh_origin_main || echo "pre-push: could not refresh origin/main (waited up to ${REFRESH_LIMIT_S}s); the range uses the local ref and may include already-merged commits -- ask GitHub: gh api repos/{owner}/{repo}/commits/main" >&2
else
    # No local ref at all: the refresh is not optional now, and the check below reports it.
    refresh_origin_main || true
fi

if ! git rev-parse --verify -q origin/main >/dev/null; then
    echo "ERROR: pre-push requires local origin/main; fetch origin main before pushing" >&2
    exit 1
fi
if ! base=$(git merge-base origin/main HEAD) || [[ -z "$base" ]]; then
    echo "ERROR: pre-push cannot establish a merge-base with origin/main" >&2
    exit 1
fi
printf '%s\n' "$base"
