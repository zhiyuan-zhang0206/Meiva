"""Concurrent-loop progress and liveness tracking for daemon health endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime


class LoopProgress:
    """Progress for one loop, closing shared-heartbeat masking by moving only
    for completed work and making ``fail()`` irreversible until respawn."""

    def __init__(
        self,
        name: str,
        timeout_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.timeout_s = timeout_s
        self._clock = clock
        self._last = self._clock()
        self._last_success_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._wedged = False

    def beat(self) -> None:
        self._last = self._clock()

    def mark_success(self) -> None:
        self._last_success_at = datetime.now(UTC).isoformat()

    def mark_error(self, message: str) -> None:
        self._last_error = {"message": message, "at": datetime.now(UTC).isoformat()}

    def fail(self, message: str) -> None:
        self._wedged = True
        self.mark_error(message)

    def stale_for(self) -> float:
        return self._clock() - self._last

    def is_alive(self) -> bool:
        return not self._wedged and self.stale_for() <= self.timeout_s

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "stale_for": self.stale_for(),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "wedged": self._wedged,
        }


class LivenessGroup:
    """Worst-case liveness across loops, closing the failure where a scheduling
    sibling keeps one shared stamp fresh while another loop is dead."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._loops: dict[str, LoopProgress] = {}

    def register(self, name: str, timeout_s: float) -> LoopProgress:
        self._loops[name] = LoopProgress(name, timeout_s, clock=self._clock)
        return self._loops[name]

    def stale_for(self) -> float:
        return max((progress.stale_for() for progress in self._loops.values()), default=0.0)

    def is_alive(self) -> bool:
        return all(progress.is_alive() for progress in self._loops.values())

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: progress.snapshot() for name, progress in self._loops.items()}
