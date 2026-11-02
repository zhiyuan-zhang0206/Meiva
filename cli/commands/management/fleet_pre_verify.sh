{home}
W="$H/pre-update-verify"; R="$W.rows"; T=""
trap 'git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -f "$R"; [ -z "$T" ] || rm -rf "$T"' EXIT
git -C "$S" worktree remove --force "$W" >/dev/null 2>&1; rm -rf "$W"; git -C "$S" worktree prune
git cat-file -e "{new}^{{commit}}" || {{ echo "SKIPPED: {new} is not fetched on this host (a dry run does not fetch)"; exit 0; }}
RCPT="$H/updates/schedules-verified"
(umask 077; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {read_rows} "$R") \
  || {{
    if [ "$(cat "$RCPT" 2>/dev/null)" = "{new}" ] && [ -n "$(find "$RCPT" -mmin -{max_age_min} 2>/dev/null)" ]; then
      echo "REUSED: the stored schedules cannot be read now; using the verdict recorded when {new} passed this check earlier in this window"
      echo "VERIFY_RC=0"; exit 0
    fi
    echo "VERIFY_RC=2"; exit 0
  }}
git -C "$S" worktree add -q "$W" {new} || {{ echo "VERIFY_RC=2"; exit 0; }}
T=$(mktemp -d) || {{ echo "VERIFY_RC=2"; exit 0; }}
cd "$W" && MEIVA_HOME="$T" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python" -c {check_rows} "$R"
rc=$?
if [ "$rc" -eq 0 ]; then mkdir -p "$H/updates" && printf '%s\n' "{new}" > "$RCPT"; fi
echo "VERIFY_RC=$rc"; exit 0
