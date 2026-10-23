# meiva:note
# Child entry: a process that stands in for the root daemon. It says it is ready, then lives
# until it is signalled. `ignore-term` also ignores SIGTERM, like a root stuck in its shutdown.

from __future__ import annotations

import signal
import sys
import time


def main() -> int:
    if sys.argv[1] == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("ready", flush=True)
    time.sleep(60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
