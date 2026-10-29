# meiva:note
# Child process for the stall-watch test: the real `run_watch` loop on a log and state file the
# test prepared, polling every 50 ms until the test ends it.

from __future__ import annotations

import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from schedules.jobs.stall_watch import StallWatch, run_watch


def main(log: str, state: str) -> None:
    run_watch(
        StallWatch(
            Path(log),
            Path(state),
            Path(state).with_name("db-write-stall-history.jsonl"),
            ZoneInfo("UTC"),
            (),
            poll_seconds=0.05,
        ),
        clock=time.time,
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
