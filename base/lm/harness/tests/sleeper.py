from __future__ import annotations

import signal
import subprocess
import sys
import time

MODES = ("sleep", "with-child", "with-detached-child", "ignore-sigterm")
LIFETIME_SECONDS = 60


# meiva:note
# A process the real-process tests start: it sleeps, and optionally starts another process (in its
# own session or not) or ignores SIGTERM. The argv is this file and a fixed mode.
def main(mode: str) -> None:
    if mode == "ignore-sigterm":
        _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _ = sys.stdout.write("ready\n")
        sys.stdout.flush()
    if mode in ("with-child", "with-detached-child"):
        _ = subprocess.Popen(  # noqa: S603
            [sys.executable, __file__, "sleep"], start_new_session=mode == "with-detached-child"
        )
    time.sleep(LIFETIME_SECONDS)


if __name__ == "__main__":
    if sys.argv[1] not in MODES:
        raise SystemExit(f"unknown mode {sys.argv[1]!r}")
    main(sys.argv[1])
