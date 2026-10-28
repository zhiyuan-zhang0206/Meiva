# meiva:note
# The POSIX "detach a child to init" spawn helper. `base.sessions.posixproc.new_session` runs it as
# `python -m base.native_process.reparent <stdout_log> <stderr_log> <cmd> [args...]` to launch a
# long-running agent process fully detached from whoever spawned it: the gateway or ops daemon that
# starts an agent is long-lived, so a naive `Popen(start_new_session=True)` agent would linger as a
# zombie in that parent after it exits, because nothing ever `waitpid()`s it (the historical
# `start_new_session` daemon-zombie bug, see `base.service_respawn`). This helper double-forks so
# the agent reparents to init (pid 1) immediately; init reaps it on death, and the spawner's only
# direct child (this helper) exits at once and is reaped by the spawner's own `subprocess` wait.
# meiva:link base/native_process/reparent/plan.py

# meiva:note
# The invocation carries the two log files and the program to exec with its arguments verbatim (the
# spawner passes JSON argument elements, so no shell is involved). This process `setsid()`s (new
# session, no controlling terminal), forks once, writes the launched child's pid to its own stdout
# (a pipe the spawner reads) and exits; the child redirects its standard streams to the two log
# files and execs the target. It runs in a fresh single-threaded interpreter, so the `os.fork()`
# here is free of the multithreaded-parent fork hazard: the spawner reaches it through
# `subprocess`' fork+exec, never a bare `os.fork()` inside the gateway's event loop. This module
# is the shell; the command line, the usage verdict and the redirect table are decided in `plan.py`
# before the fork.

from __future__ import annotations

import os
import sys

from base.native_process.reparent.plan import (
    LOG_MODE,
    LOG_OPEN_FLAGS,
    USAGE,
    Redirects,
    parse_argv,
    redirect_plan,
)


# meiva:note
# Runs in the grandchild after the fork: point the three standard streams at the log files and exec
# the target. Only async-signal-safe `os.*` calls and no Python-level locks, so it is safe
# regardless of what the forking process held. stdin is /dev/null (the agent never reads it);
# stdout and stderr go to the log files so a pre-exec failure or a C-level traceback survives.
# Redirecting fd 1 also closes the pid pipe inherited from the spawner, which then reads EOF.
def _exec_child(table: Redirects, command: tuple[str, ...]) -> None:
    out_fd = os.open(table.stdout_log, LOG_OPEN_FLAGS, LOG_MODE)
    err_fd = (
        out_fd if table.stderr_log is None else os.open(table.stderr_log, LOG_OPEN_FLAGS, LOG_MODE)
    )
    null_fd = os.open(table.stdin_device, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.dup2(out_fd, 1)
    os.dup2(err_fd, 2)
    # meiva:note
    # `command[0]` is the absolute interpreter path the spawner built from repository internals,
    # and the child inherits this process's environment on purpose: the spawner already filtered it.
    os.execv(command[0], command)  # noqa: S606


def main(argv: list[str]) -> None:
    args = parse_argv(argv)
    if args is None:
        sys.stderr.write(USAGE)
        os._exit(2)
    table = redirect_plan(args)
    # meiva:note
    # New session first: detach from any controlling terminal, so a terminal SIGHUP can never reach
    # the agent. `subprocess` spawned this helper as a plain child (not a group leader), so
    # `setsid` always succeeds.
    os.setsid()
    pid = os.fork()
    if pid > 0:
        # meiva:note
        # Intermediate: hand the agent's pid back to the spawner over the fd-1 pipe, then exit so
        # the agent reparents to init. `os.write` is async-signal-safe; nothing is buffered.
        os.write(1, str(pid).encode())
        os._exit(0)
    _exec_child(table, args.command)


if __name__ == "__main__":
    main(sys.argv[1:])
