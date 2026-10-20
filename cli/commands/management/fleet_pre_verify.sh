{home}
W="$H/pre-update-verify"; R="$W.rows"; T=""
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -f "$R"; [ -z "$T" ] || rm -rf "$T"' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
git cat-file -e "{new}^{{commit}}" || {{ echo "SKIPPED: {new} is not fetched on this host (a dry run does not fetch)"; exit 0; }}
(umask 077; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {read_rows} "$R") \
  || {{ echo "VERIFY_RC=2"; exit 0; }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "VERIFY_RC=2"; exit 0; }}
T=$(mktemp -d) || {{ echo "VERIFY_RC=2"; exit 0; }}
cd "$W" && MEIVA_HOME="$T" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {check_rows} "$R"
echo "VERIFY_RC=$?"; exit 0
