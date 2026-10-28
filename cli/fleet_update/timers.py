# meiva:note
# The process clock a window measures its waits with, read in one place so nothing else under
# `cli.fleet_update` touches `time` for itself. A window runs from a bare operator checkout, so this
# wraps `time` rather than borrowing the application's clock.
from __future__ import annotations

import time


class ProcessTimers:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)
