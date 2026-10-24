from __future__ import annotations

import asyncio
import threading
import time

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.daemon.health import LoopProgress
from base.daemon.round_loop import CONSECUTIVE_ROUND_FAILURE_LIMIT, RoundFailureLimitError
from services.upkeep.events_maintenance import passes


# meiva:note
# A pass that follows `outcomes` in order (None completes, an exception is raised), then waits for
# the test to stop the loop. `calls` holds the instant each pass started.
class _Script:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = outcomes
        self.calls: list[float] = []
        self.stop = threading.Event()

    def __call__(self) -> None:
        self.calls.append(time.monotonic())
        index = len(self.calls) - 1
        if index >= len(self.outcomes):
            self.stop.wait(10.0)
            return
        outcome = self.outcomes[index]
        if outcome is not None:
            raise outcome


def _progress(timeout_s: float = 5.0) -> LoopProgress:
    return LoopProgress("dispatch", timeout_s=timeout_s, timers=SYSTEM_TIMERS)


async def test_consecutive_failed_passes_end_the_loop() -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    script = _Script([RuntimeError("boom")] * (limit + 3))
    calls = script.calls

    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.TaskGroup() as tasks:
            await passes.pass_loop(
                _progress(), label="rollup", interval_s=0.001, work=script, tasks=tasks
            )

    assert raised.group_contains(RoundFailureLimitError)
    assert len(calls) == limit, "the loop must stop at the limit, not keep retrying"


async def test_a_completed_pass_and_a_db_outage_do_not_exhaust_the_budget() -> None:
    limit = CONSECUTIVE_ROUND_FAILURE_LIMIT
    boom = RuntimeError("boom")
    outcomes: list[BaseException | None] = [boom] * (limit - 1)
    outcomes += [None]
    outcomes += [psycopg.OperationalError("db down")] * limit
    outcomes += [boom] * (limit - 1)
    script = _Script(outcomes)
    calls = script.calls

    async with asyncio.TaskGroup() as tasks:
        loop = tasks.create_task(
            passes.pass_loop(
                _progress(), label="rollup", interval_s=0.001, work=script, tasks=tasks
            )
        )
        deadline = time.monotonic() + 20.0
        while len(calls) <= len(outcomes) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        loop.cancel()
        script.stop.set()

    assert len(calls) == len(outcomes) + 1, "every scripted pass ran and the loop was still going"


# meiva:note
# A transient failure is recorded and then waits out the interval, so a database blip does not
# become a tight hot-loop against Postgres.
async def test_a_failed_pass_waits_a_full_interval_before_retrying() -> None:
    progress = _progress()
    script = _Script([RuntimeError("transient db blip")])
    calls = script.calls

    async with asyncio.TaskGroup() as tasks:
        loop = tasks.create_task(
            passes.pass_loop(progress, label="rollup", interval_s=0.4, work=script, tasks=tasks)
        )
        deadline = time.monotonic() + 20.0
        while len(calls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        loop.cancel()
        script.stop.set()

    assert calls[1] - calls[0] >= 0.4
    last_error = progress.snapshot()["last_error"]
    assert isinstance(last_error, dict) and last_error["message"] == "transient db blip"


async def test_an_unreachable_database_is_recorded_and_retried_after_the_interval() -> None:
    progress = _progress()
    script = _Script([psycopg.OperationalError("db down")])
    calls = script.calls

    async with asyncio.TaskGroup() as tasks:
        loop = tasks.create_task(
            passes.pass_loop(progress, label="rollup", interval_s=0.2, work=script, tasks=tasks)
        )
        deadline = time.monotonic() + 20.0
        while len(calls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        loop.cancel()
        script.stop.set()

    assert calls[1] - calls[0] >= 0.2
    last_error = progress.snapshot()["last_error"]
    assert isinstance(last_error, dict) and last_error["message"] == "db down"


async def test_schema_drift_ends_the_loop_with_the_original_error() -> None:
    drift = psycopg.ProgrammingError("drift")
    script = _Script([drift])
    calls = script.calls

    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.TaskGroup() as tasks:
            await passes.pass_loop(
                _progress(), label="resolution", interval_s=0.001, work=script, tasks=tasks
            )

    assert raised.value.exceptions == (drift,)
    assert len(calls) == 1


async def test_a_wedged_pass_parks_the_loop_without_waiting_out_the_interval() -> None:
    release = threading.Event()

    def overrun() -> None:
        release.wait(10.0)

    progress = _progress(timeout_s=0.1)
    started = time.monotonic()
    try:
        async with asyncio.TaskGroup() as tasks:
            await asyncio.wait_for(
                passes.pass_loop(
                    progress, label="rollup", interval_s=60.0, work=overrun, tasks=tasks
                ),
                timeout=5.0,
            )
            release.set()
    finally:
        release.set()

    assert time.monotonic() - started < 5.0
    assert progress.snapshot()["wedged"] is True
