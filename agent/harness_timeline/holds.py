from __future__ import annotations

from collections.abc import Iterable

HOLD_SECONDS = 120.0
ABANDONED_WINDOW_ROWS = 5_000
RELEASE_ROUNDS = 8


# meiva:note
# One clock per result whose call has not been read: when it was first seen held (the writer's monotonic clock), so a result
# is shown top-level when its own `HOLD_SECONDS` have passed and not when another's have. The clock is made from what the
# projector says is held after every read and after a rebuild, so a result a replay finds held starts counting when the
# replay ends, with or without a new batch to come. The output of a sub-agent whose link has not been read is on no clock:
# a foreground sub-agent's link arrives with the result of the call that started it, however long it runs.
# meiva:link agent/harness_timeline/README.md
class OrphanClocks:
    def __init__(self) -> None:
        self._since: dict[str, float] = {}

    def track(self, held: Iterable[str], now: float) -> None:
        self._since = {call: self._since.get(call, now) for call in held}

    def expired(self, now: float) -> frozenset[str]:
        return frozenset(call for call, since in self._since.items() if now - since >= HOLD_SECONDS)


# meiva:note
# Where a restart replays a record from, at a quiet point: the last stored row, held back to just before the oldest call of the
# record that was abandoned (its turn closed) and has no result yet, among those in the latest `ABANDONED_WINDOW_ROWS` rows (a
# replay is at most that many rows), and never moved back from where it was. `oldest` is that call's row (None when there is
# none). A rebuilt projector then knows the call when its result arrives after a restart and pairs it. A call older than the
# window is no longer waited for across a restart, and does not take the newer ones with it: its late result is held, then
# shown top-level (`orphan_released`).
def restart_point(last: int, before: int, oldest: int | None) -> int:
    if oldest is None:
        return last
    return max(before, min(last, oldest - 1))
