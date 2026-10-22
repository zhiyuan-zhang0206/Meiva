#!/usr/bin/env bash
# Wait for the dpkg frontend lock before an install attempt takes it.
#
# A bounded retry can start its next attempt while the previous attempt's apt is still
# alive — `timeout` kills the sudo child, not the apt it started — and the collision then
# reads as another failed mirror. The wait names the holder so the log says what happened,
# but it is ADVISORY: exit 1/2 mean "could not clear a lock", and callers keep going to the
# cached-archive path, which is what saves a job when the mirror is down.
set -euo pipefail

timeout_seconds="${1:-120}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rc=0
sudo timeout $((timeout_seconds + 60)) python3 "${script_dir}/apt_lock.py" --timeout "${timeout_seconds}" || rc=$?
case "${rc}" in
  0) exit 0 ;;
  1)
    echo "::warning::apt still holds the dpkg lock after ${timeout_seconds}s — a stuck apt, not a mirror or cache problem (the holder pid is on the line above); continuing to the cached archives" >&2
    exit 1
    ;;
  *)
    echo "::warning::could not inspect the dpkg lock (rc=${rc}); see the line above; continuing" >&2
    exit "${rc}"
    ;;
esac
