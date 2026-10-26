from __future__ import annotations

import asyncio
from collections.abc import Callable

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig, Timers
from base.daemon.health import DaemonProbe
from base.log.tests.log_records import LogRecords
from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root import manifest as manifest_mod
from services.supervision.meiva_root.failure_state import UnitFailureFacts

__all__ = ["CellProbe", "FakeClock", "Recorder", "StubSupervisor", "clock_over", "system_clock"]


# meiva:note
# The real wall clock and timers, as the root daemon builds them: a test that does not steer time
# passes this, one that does builds a `Clock` over `ManualTimers`.
def system_clock() -> Clock:
    return Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=SYSTEM_TIMERS)


# meiva:note
# A `Clock` over a test's own timers: its monotonic reads and sleeps follow the fake, and its wall
# time stays the real one.
def clock_over(timers: Timers) -> Clock:
    return Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=timers)


class StubSupervisor:
    def __init__(
        self,
        deferrals: dict[str, str] | None = None,
        *,
        unknown_units: set[str] | None = None,
    ) -> None:
        self.restart_calls: list[str] = []
        self.deferrals: dict[str, str] = dict(deferrals or {})
        self.unknown_units: set[str] = set(unknown_units or ())
        self.on_restart: Callable[[], None] | None = None
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.failure_facts = UnitFailureFacts(intent_running=True, restart_failed=None)

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        if self.on_restart is not None:
            self.on_restart()
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.deferrals.get(unit_id)

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        if unit_id in self.unknown_units:
            raise manifest_mod.UnknownUnitError(f"unknown unit {unit_id!r}")
        return self.failure_facts


class CellProbe:
    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict
        self.calls = 0

    def __call__(self) -> DaemonProbe:
        self.calls += 1
        return self.verdict


class FakeClock:
    def __init__(self, start: float = 1000.0, step: float = 0.02) -> None:
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [{"message": r["message"], **r["extra"]} for r in self._records]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]
