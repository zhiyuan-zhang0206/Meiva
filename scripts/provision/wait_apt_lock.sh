#!/usr/bin/env bash
# Wait for the dpkg frontend lock before an install attempt takes it.
#
# A bounded retry can start its next attempt while the previous attempt's apt is still
# alive — `timeout` kills the sudo child, not the apt it started — and the collision then
# reads as another failed mirror. Waiting on the same fcntl lock apt uses, with a ceiling,
# keeps a stuck neighbour from being reported as a package or cache failure.
set -euo pipefail

timeout_seconds="${1:-120}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rc=0
sudo timeout $((timeout_seconds + 60)) python3 "${script_dir}/apt_lock.py" --timeout "${timeout_seconds}" || rc=$?
case "${rc}" in
  0) exit 0 ;;
  1)
    echo "::error::apt still holds the dpkg lock after ${timeout_seconds}s — a stuck apt, not a mirror or cache problem (the holder pid is on the line above)" >&2
    exit 1
    ;;
  *)
    echo "::error::could not inspect the dpkg lock (rc=${rc}); see the line above" >&2
    exit "${rc}"
    ;;
esac
