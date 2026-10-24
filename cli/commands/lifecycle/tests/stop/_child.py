# meiva:note
# Child entry of the stop tests: the processes a stop must close, or leave alone.
#
#   session TERM HANGUP ARMED   a private session leader; says "ready" once its signals are set.
#                               TERM is default|exit|ignore; HANGUP is default|ignore; a non-empty
#                               ARMED starts a child that ignores SIGTERM and touches that path.
#   armed-child PATH            that child.
#   sleep SECONDS [CHILD_S]     lives SECONDS; with CHILD_S it first starts a child that lives
#                               that long and outlasts it.

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType


def _exit_cleanly(_signum: int, _frame: FrameType | None) -> None:
    sys.exit(0)


def session(term: str, hangup: str, armed: str) -> None:
    if term == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif term == "exit":
        signal.signal(signal.SIGTERM, _exit_cleanly)
    elif term != "default":
        raise ValueError("invalid fixture SIGTERM behavior")
    if hangup == "ignore":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if armed:
        subprocess.Popen([sys.executable, "-u", __file__, "armed-child", armed])
    print("ready", flush=True)
    time.sleep(60)


def armed_child(path: str) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(path).touch()
    time.sleep(60)


def sleep(seconds: str, child_seconds: str | None) -> None:
    if child_seconds is not None:
        subprocess.Popen([sys.executable, "-u", __file__, "sleep", child_seconds])
    time.sleep(float(seconds))


def main(argv: list[str]) -> None:
    match argv:
        case ["session", term, hangup, armed]:
            session(term, hangup, armed)
        case ["armed-child", path]:
            armed_child(path)
        case ["sleep", seconds]:
            sleep(seconds, None)
        case ["sleep", seconds, child_seconds]:
            sleep(seconds, child_seconds)
        case _:
            raise SystemExit(f"unknown child command: {argv}")


if __name__ == "__main__":
    main(sys.argv[1:])
