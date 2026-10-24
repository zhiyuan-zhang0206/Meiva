from __future__ import annotations

import asyncio
import logging
import threading
import time

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.daemon.health import LoopProgress
from services.upkeep.events_maintenance import passes


@pytest.mark.parametrize(
    "failure", [RuntimeError("unknown failure"), psycopg.ProgrammingError("drift")]
)
async def test_completed_failure_preserves_the_original_exception(failure: Exception) -> None:
    progress = LoopProgress("dispatch", timeout_s=1.0, timers=SYSTEM_TIMERS)

    def fail() -> None:
        raise failure

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(type(failure)) as raised:
            await passes.maintenance_with_liveness(progress, fail, tasks=tasks)
        assert raised.value is failure
        assert not progress.snapshot()["wedged"]


async def test_late_failure_is_reported_without_reviving_the_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    progress = LoopProgress("dispatch", timeout_s=0.01, timers=SYSTEM_TIMERS)
    release = threading.Event()
    failure = RuntimeError("late worker failure")

    def fail_after_release() -> None:
        assert release.wait(5.0), "test did not release its worker"
        raise failure

    with caplog.at_level(logging.ERROR, logger=passes.events_maintenance_log.name):
        try:
            async with asyncio.TaskGroup() as tasks:
                with pytest.raises(passes.WedgedPassError):
                    await passes.maintenance_with_liveness(
                        progress, fail_after_release, tasks=tasks
                    )
                assert progress.snapshot()["wedged"]
                release.set()
        finally:
            release.set()

    records = [record for record in caplog.records if "after its hard deadline" in record.message]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].exc_info[1] is failure
    assert records[0].exc_info[2] is not None
    assert progress.snapshot()["wedged"]


# meiva:note
# A worker that exceeds its hard deadline wedges; elapsed time never earns a beat.
async def test_wedged_pass_fails_without_earning_a_beat_while_in_flight() -> None:
    progress = LoopProgress("dispatch", timeout_s=0.1, timers=SYSTEM_TIMERS)

    def overrun() -> None:
        time.sleep(0.4)

    async with asyncio.TaskGroup() as tasks:
        with pytest.raises(passes.WedgedPassError, match=r"dispatch.*hard deadline"):
            await passes.maintenance_with_liveness(progress, overrun, tasks=tasks)

    assert progress.stale_for() >= 0.1, "a beat during the overrun would have reset the clock"
    assert not progress.is_alive()
    assert progress.snapshot()["wedged"] is True


# meiva:note
# A bounded worker completion advances progress after it returns, never before.
async def test_completed_pass_beats_after_its_worker_finishes() -> None:
    progress = LoopProgress("dispatch", timeout_s=5.0, timers=SYSTEM_TIMERS)

    def work() -> None:
        time.sleep(0.4)

    async with asyncio.TaskGroup() as tasks:
        await passes.maintenance_with_liveness(progress, work, tasks=tasks)

    assert progress.stale_for() < 0.2, "the beat came before the worker finished"


async def test_sleeping_beats_progress_so_a_long_wait_is_not_a_wedge() -> None:
    progress = LoopProgress("dispatch", timeout_s=0.3, timers=SYSTEM_TIMERS)
    await asyncio.sleep(0.4)
    assert not progress.is_alive()

    await passes.sleep_with_liveness(progress, 0.05)

    assert progress.is_alive()
