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

if [[ "$full" == true ]]; then
    .venv/bin/python -m meizel check
elif [[ "${#changed[@]}" -gt 0 ]]; then
    .venv/bin/python -m meizel check --changed "${changed[@]}"
else
    echo 'pre-push: no Python, manifest or role-list input changed; skipping meizel'
fi
