---
type: doc
title: Watcher launch line
description: "How a watcher's generated launch line is sourced into its session shell."
tags: []
---

# Watcher launch line

A watcher's session is never handed the command text directly. The generated
launch line is written to `watcher_<session_id>_launch.sh` beside the watcher's
script, and the session receives only `. <that path>`; the file removes itself
(`command rm -f -- <path>`) and then runs the line.

Sourcing keeps long absolute paths and the notification pipeline out of the
terminal input stream. A fresh PTY can still be in canonical mode while the login
shell reads its profile, and that input buffer truncates a long line before
readline takes over (observed at 1024 bytes on macOS). Sourcing preserves the
command's exit status, the session's own exit behaviour and the watcher child's
orphan-guard parent; the shared launch-line reasoning lives in
[[meiva/shell/docs/shell.meiva.okf.md|the shell background contract]].

If the line cannot be sent, the session is killed and the launch file removed: a
session whose launch command never arrived is not a watcher, and leaving it would
sit under the watcher's name (up to the TTL's 7 days for a default cron) with
nothing to run.

Parent: [[meiva/watcher/docs/watcher.meiva.okf.md]].
