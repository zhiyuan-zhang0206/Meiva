#!/usr/bin/env bash
# Python or manifest changes need the full graph, including stale declarations.
# Other role-list inputs still judge global generated regions and package-list copies,
# with only the branch's changed facts. Deleted paths and both sides of renames count.
set -euo pipefail

base_sha="$(bash "$(dirname "$0")/prepush-base.sh")"
path_file="$(mktemp)"
trap 'rm -f "$path_file"' EXIT
# NUL framing preserves spaces, newlines and leading dashes in tracked names. A failed
# diff exits before any command can report a successful empty contribution.
git diff --name-only --no-renames -z "$base_sha" HEAD > "$path_file"
changed=()
full=false
while IFS= read -r -d '' path; do
    case "$path" in
        *.py|meizel.toml|*/meizel.toml) full=true; changed+=("$path") ;;
        *.sh|*.toml|*.yaml|*.yml) changed+=("./$path") ;;
    esac
done < "$path_file"

# meizel exits 3 when the checkout's frontend dependencies are missing or stale: the checker could
# not answer a TypeScript question, which is a property of this tree, not of the push.
run_meizel() {
    local rc=0
    .venv/bin/python -m meizel check "$@" || rc=$?
    if [[ "$rc" -eq 3 ]]; then
        echo "WARNING: PRE-PUSH SKIPPED [meizel]: frontend dependencies are not ready for the TypeScript analyzer (reason above). CI must pass before merge." >&2
        return 0
    fi
    return "$rc"
}

if [[ "$full" == true ]]; then
    run_meizel
elif [[ "${#changed[@]}" -gt 0 ]]; then
    run_meizel --changed "${changed[@]}"
else
    echo 'pre-push: no Python, manifest or role-list input changed; skipping meizel'
fi
