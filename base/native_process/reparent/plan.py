# meiva:note
# The reparenting helper's decisions, taken over plain values and before the fork: whether the
# command line names a program to run, and where the launched program's three standard streams
# point.
# meiva:link base/native_process/reparent/__main__.py

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

USAGE = "usage: _reparent <stdout_log> <stderr_log> <cmd> [args...]\n"

# meiva:note
# The log files are opened append-only, because two helpers may be launched for the same path, and
# world-readable like the files a shell redirection would create.
LOG_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND
LOG_MODE = 0o644

_STDIN_DEVICE = os.devnull


@dataclass(frozen=True)
class ReparentArgs:
    stdout_log: str
    stderr_log: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class Redirects:
    stdin_device: str
    stdout_log: str
    stderr_log: str | None


# meiva:note
# None when the line is too short to name a program: the helper prints `USAGE` and exits 2.
def parse_argv(argv: Sequence[str]) -> ReparentArgs | None:
    if len(argv) < 3:
        return None
    return ReparentArgs(stdout_log=argv[0], stderr_log=argv[1], command=tuple(argv[2:]))


# meiva:note
# stdin reads the null device; stderr shares stdout's descriptor when both paths name the same
# file, so that file is opened once.
def redirect_plan(args: ReparentArgs) -> Redirects:
    return Redirects(
        stdin_device=_STDIN_DEVICE,
        stdout_log=args.stdout_log,
        stderr_log=None if args.stderr_log == args.stdout_log else args.stderr_log,
    )
