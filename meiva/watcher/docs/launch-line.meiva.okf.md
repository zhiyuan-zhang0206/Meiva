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

The launch is split into decisions over values and a shell that executes them.
`meiva/watcher/launch_plan.py` (strict) plans the three generated files, the command
and the single launch line; `meiva/watcher/spawn.py:run` writes them, sends the line to
the agent's real shell session and, on any failure after the session exists, removes
the files and kills the session. Tests assert the plan on values and run the shell
against the real PTY service: the notice CLI is a value in the deps (a script that
records its argv), the long-path case points the workspace at a long directory, the
stale-identity case supplies the wrong identity through the runner environment (the
shipped bootstrap must overwrite it before SDK import), and the failed-launch cases make
one generated path a directory.

Stale generated files are cleaned inside `run`, after the session exists, in a fixed
order: create the session, list the watchers directory, list the live sessions, remove
what `launch_plan.stale_names` names. The order is the Bug A (#1116) fix. A sibling that
starts at the same moment creates its session before it writes a file, so every file the
directory listing shows belongs to a session the later listing contains, and its child's
unread files are never taken for dead ones; a live-session snapshot taken earlier (while
the deps were built) misses such a sibling. The decision is a strict function over the two
listings; `files.scan` and `files.remove` are the only disk steps, and a failure in any of
them kills the session like every later step.

Parent: [[meiva/watcher/docs/watcher.meiva.okf.md]].
