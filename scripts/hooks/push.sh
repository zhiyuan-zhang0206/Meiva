#!/usr/bin/env bash
# meiva:note — push with the hook output going to a file, not to a pipe.
#
# OpenSSH sets O_NONBLOCK on its inherited stderr for the whole push session. When
# the caller merged stdout and stderr into one pipe (`git push 2>&1 | tail`), that
# flag lives on the shared open file description, so pre-commit's burst of captured
# hook output raises BlockingIOError and the push aborts — after the hooks already
# ran, which is why `--no-verify` looked like the only way out (task #5494). A
# regular file ignores O_NONBLOCK, so the same hooks complete.
#
# Usage: scripts/push.sh [git push arguments...]
set -uo pipefail
log="$(mktemp -t meiva-push.XXXXXX)" || exit 1
trap 'rm -f "$log"' EXIT

push() {
    git push "$@" >"$log" 2>&1
}

push "$@"
rc=$?
if grep -q 'BlockingIOError' "$log"; then
    echo "push: the hook output hit EAGAIN (BlockingIOError); retrying once" >&2
    push "$@"
    rc=$?
fi
tail -n 40 "$log"
exit "$rc"
