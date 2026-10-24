# meiva:note
# A child process for tests that need a live process of their own to point at: it sleeps until
# it is killed.
#
# With `undumpable` it first clears its dumpable flag, which on Linux makes its working directory
# unreadable to every other process of its user (as a process that changed credentials is), and
# says `ready` once it has. Nothing else runs here.

from __future__ import annotations

import ctypes
import sys
import time

_PR_SET_DUMPABLE = 4
_SLEEP_S = 60


def main(argv: list[str]) -> None:
    if argv == ["undumpable"]:
        ctypes.CDLL(None).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
        print("ready", flush=True)
    time.sleep(_SLEEP_S)


if __name__ == "__main__":
    main(sys.argv[1:])
