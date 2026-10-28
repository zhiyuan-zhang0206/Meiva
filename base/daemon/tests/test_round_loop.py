from __future__ import annotations

import asyncio

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress

# meiva:note
# Generous ceiling for "the loop made the expected progress": it bounds a genuinely stuck
# loop without turning the assertion into a measurement of the runner's scheduling latency.
_ROUND_PROGRESS_TIMEOUT_S = 5.0


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0, timers=SYSTEM_TIMERS)


async def test_a_round_that_raises_ends_the_loop() -> None:
    async def one_round() -> None:
        raise ValueError("unexpected")

    with pytest.raises(ValueError):
        await round_loop.run_rounds("t", _progress(), 0.01, one_round)


async def test_an_unreachable_database_skips_the_round_and_the_loop_goes_on() -> None:
    progress = _progress()
    outcomes = [psycopg.OperationalError("db down"), None, None]
    seen = 0
    third_started = asyncio.Event()

    async def one_round() -> None:
        nonlocal seen
        seen += 1
        if seen >= 3:
            third_started.set()
        outcome = outcomes[seen - 1]
        if outcome is not None:
            raise outcome

    task = asyncio.create_task(round_loop.run_rounds("t", progress, 0.01, one_round))
    try:
        # meiva:note
        # Wait for observable progress rather than a fixed wall-clock window: a loaded
        # runner can miss 0.2s of scheduling without the loop doing anything wrong.
        await asyncio.wait_for(third_started.wait(), timeout=_ROUND_PROGRESS_TIMEOUT_S)
        assert seen >= 3
        assert progress.snapshot()["last_error"] is not None
        assert progress.snapshot()["last_success_at"] is not None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rounds_are_strictly_sequential() -> None:
    running = 0
    peak = 0
    done = 0
    three_done = asyncio.Event()

    async def one_round() -> None:
        nonlocal running, peak, done
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.03)
        running -= 1
        done += 1
        if done >= 3:
            three_done.set()

    task = asyncio.create_task(round_loop.run_rounds("t", _progress(), 0.0, one_round))
    try:
        # meiva:note
        # Three rounds cost ~0.09s of work, so a fixed 0.2s sleep made this assertion a
        # race against the scheduler instead of a statement about sequencing.
        await asyncio.wait_for(three_done.wait(), timeout=_ROUND_PROGRESS_TIMEOUT_S)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done >= 3
    assert peak == 1


async def test_fan_out_bounds_concurrency_and_returns_when_all_are_done() -> None:
    running = 0
    peak = 0
    finished: list[int] = []

    def job(index: int):
        async def run() -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            finished.append(index)

        return run

    await round_loop.fan_out([job(i) for i in range(6)], concurrency=2, progress=_progress())

    assert sorted(finished) == list(range(6))
    assert peak == 2


async def test_fan_out_failure_cancels_the_round() -> None:
    cancelled = asyncio.Event()

    async def failing() -> None:
        await asyncio.sleep(0.01)
        raise RuntimeError("job failed")

    async def parked() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(ExceptionGroup):
        await round_loop.fan_out([failing, parked], concurrency=2, progress=_progress())

    assert cancelled.is_set()


async def test_a_callable_interval_is_read_after_every_round() -> None:
    reads = 0

    def interval() -> float:
        nonlocal reads
        reads += 1
        return 0.001

    async def one_round() -> None:
        return None

    task = asyncio.create_task(round_loop.run_rounds("t", _progress(), interval, one_round))
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert reads >= 2


async def test_an_interval_that_raises_ends_the_loop() -> None:
    def interval() -> float:
        raise RuntimeError("config unreadable")

    async def one_round() -> None:
        return None

    with pytest.raises(RuntimeError):
        await round_loop.run_rounds("t", _progress(), interval, one_round)


def test_a_failure_budget_raises_at_its_limit_chained_to_the_last_error() -> None:
    budget = round_loop.RoundFailureBudget("t", limit=3)
    budget.failed(ValueError("one"))
    budget.failed(ValueError("two"))
    last = ValueError("three")

    with pytest.raises(round_loop.RoundFailureLimitError, match="3 consecutive") as raised:
        budget.failed(last)

    assert raised.value.__cause__ is last


def test_a_completed_round_clears_the_failure_count() -> None:
    budget = round_loop.RoundFailureBudget("t", limit=3)
    for _ in range(5):
        budget.failed(ValueError("a"))
        budget.failed(ValueError("b"))
        budget.succeeded()
