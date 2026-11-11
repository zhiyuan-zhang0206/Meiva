{home}
W="$H/pre-update-root-check"
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
(git cat-file -e "{new}^{{commit}}" || git fetch -q origin {new}) \
  || {{ echo "SKIPPED: {new} cannot be fetched on this host"; exit 0; }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "ROOT_CHECK_RC=2"; exit 0; }}
cd "$W" && MEIVA_HOME="$H" "$S/.venv/bin/python" -c {program} "$S" {selection}
echo "ROOT_CHECK_RC=$?"; exit 0
