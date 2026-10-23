from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import psycopg
import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.daemon.loop_health import LoopProgress
from base.daemon.round_loop import CONSECUTIVE_ROUND_FAILURE_LIMIT, RoundFailureLimitError
from services.wake.heartbeat import loops


class _Script:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes
        self.rounds = 0

    def advance(self) -> None:
        if self.rounds == len(self.outcomes):
            raise asyncio.CancelledError
        outcome = self.outcomes[self.rounds]
        self.rounds += 1
        if outcome is not None:
            raise outcome


class _Dispatch:
    interval_s = 15.0

    def __init__(self, script: _Script) -> None:
        self._script = script

    def run_round(self) -> None:
        self._script.advance()


class _Liveness:
    def __init__(self, script: _Script) -> None:
        self._script = script

    async def run_round(self) -> None:
        self._script.advance()


def _drive(loop: str, script: _Script) -> Awaitable[None]:
    timers = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    progress = LoopProgress(loop, 60.0, timers=clock)
    if loop == "poll":
        return loops.dispatch_loop(_Dispatch(script), progress, clock=clock)
    return loops.liveness_loop(_Liveness(script), progress, clock=clock)


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_consecutive_failed_rounds_end_the_loop(loop: str) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    script = _Script([RuntimeError("boom")] * (limit + 3))
    with pytest.raises(RoundFailureLimitError):
        await _drive(loop, script)
    assert script.rounds == limit, "the loop must stop at the limit, not keep retrying"


@pytest.mark.parametrize("loop", ["poll", "liveness"])
async def test_a_completed_round_and_a_db_outage_do_not_exhaust_the_budget(loop: str) -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    boom = RuntimeError("boom")
    outcomes: list[BaseException | None] = [boom] * (limit - 1)
    outcomes += [None]
    outcomes += [psycopg.OperationalError("db down")] * limit
    outcomes += [boom] * (limit - 1)
    script = _Script(outcomes)
    with pytest.raises(asyncio.CancelledError):
        await _drive(loop, script)
    assert script.rounds == len(outcomes)


async def test_a_dispatch_schema_error_ends_the_first_round() -> None:
    error = psycopg.ProgrammingError("schema mismatch")
    script = _Script([error] * CONSECUTIVE_ROUND_FAILURE_LIMIT)
    with pytest.raises(psycopg.ProgrammingError) as caught:
        await _drive("poll", script)
    assert caught.value is error
    assert script.rounds == 1
