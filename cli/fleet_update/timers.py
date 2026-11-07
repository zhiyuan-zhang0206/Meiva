# meiva:note
# The process clock a window measures its waits with, read in one place so nothing else under
# `cli.fleet_update` touches `time` for itself.
from __future__ import annotations

import time


class ProcessTimers:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
