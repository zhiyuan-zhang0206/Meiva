"""Concurrent-loop progress and liveness tracking for daemon health endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from base.clock import Timers


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LoopProgress:
    """Progress for one loop, closing shared-heartbeat masking by moving only
    for completed work and making ``fail()`` irreversible until respawn."""

    def __init__(self, name: str, timeout_s: float, *, timers: Timers) -> None:
        self._timers = timers
        self.name = name
        self.timeout_s = timeout_s
        self._last = self._timers.monotonic()
        self._last_success_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._wedged = False
        self.consecutive_failures = 0

    def beat(self) -> None:
        self._last = self._timers.monotonic()

    def mark_success(self) -> None:
        self._last_success_at = _now_iso()
        self.consecutive_failures = 0

    def mark_error(self, message: str) -> None:
        self._last_error = {"message": message, "at": _now_iso()}
        self.consecutive_failures += 1

    def fail(self, message: str) -> None:
        self._wedged = True
        self.mark_error(message)

    def stale_for(self) -> float:
        return self._timers.monotonic() - self._last

    def last_error_message(self) -> str | None:
        return None if self._last_error is None else self._last_error["message"]

    def last_success_at(self) -> str | None:
        return self._last_success_at

    def is_alive(self) -> bool:
        return not self._wedged and self.stale_for() <= self.timeout_s

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "stale_for": self.stale_for(),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "consecutive_failures": self.consecutive_failures,
            "wedged": self._wedged,
        }


class LivenessGroup:
    """Worst-case liveness across loops, closing the failure where a scheduling
    sibling keeps one shared stamp fresh while another loop is dead."""

    def __init__(self, *, timers: Timers) -> None:
        self._timers = timers
        self._loops: dict[str, LoopProgress] = {}

    def register(self, name: str, timeout_s: float) -> LoopProgress:
        self._loops[name] = LoopProgress(name, timeout_s, timers=self._timers)
        return self._loops[name]

    def stale_for(self) -> float:
        return max((progress.stale_for() for progress in self._loops.values()), default=0.0)

    def is_alive(self) -> bool:
        return all(progress.is_alive() for progress in self._loops.values())

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: progress.snapshot() for name, progress in self._loops.items()}

    def progresses(self) -> dict[str, LoopProgress]:
        return dict(self._loops)
