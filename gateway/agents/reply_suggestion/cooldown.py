from __future__ import annotations

from datetime import datetime, timedelta

__all__ = ["WARNING_COOLDOWN", "WarningCooldown"]

WARNING_COOLDOWN = timedelta(minutes=5)


# meiva:note
# At most one warning per key per cooldown window, counting the repeats it hides.
#
# `admit` answers whether this occurrence warns: None when the key warned within the window,
# otherwise the number of occurrences suppressed since its last warning. The caller supplies
# the time, so the window follows the request clock.
class WarningCooldown:
    def __init__(self, window: timedelta = WARNING_COOLDOWN) -> None:
        self._window = window
        self._last: dict[str, datetime] = {}
        self._suppressed: dict[str, int] = {}

    def admit(self, key: str, now: datetime) -> int | None:
        last = self._last.get(key)
        if last is not None and now - last < self._window:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        self._last[key] = now
        return self._suppressed.pop(key, 0)
