from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from services.derived.embeddinggemma.scheduler import InferenceScheduler, SchedulerClosedError


@pytest.mark.asyncio
async def test_single_input_passes_a_queued_batch_and_batches_keep_making_progress() -> None:
    calls: list[str] = []
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()

    def infer(name: str) -> Callable[[], list[list[float]]]:
        def run() -> list[list[float]]:
            calls.append(name)
            if name == "active":
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("test did not release the inference")
            return [[1.0]]

        return run

    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = InferenceScheduler(executor)
        active = asyncio.create_task(scheduler.run(infer("active"), single=False))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            jobs = [
                asyncio.create_task(scheduler.run(infer(name), single=single))
                for name, single in [
                    ("batch1", False),
                    ("batch2", False),
                    ("online1", True),
                    ("online2", True),
                    ("online3", True),
                ]
            ]
            await asyncio.sleep(0)
            assert calls == ["active"]
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(active, *jobs), timeout=5)

    assert calls == ["active", "online1", "batch1", "online2", "batch2", "online3"]


@pytest.mark.asyncio
async def test_cancelled_requests_and_shutdown_do_not_submit_more_inference() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()

    def active_inference() -> list[list[float]]:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("test did not release the inference")
        return []

    def unexpected() -> list[list[float]]:
        pytest.fail("queued inference ran after cancellation or shutdown")

    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = InferenceScheduler(executor)
        active = asyncio.create_task(scheduler.run(active_inference, single=False))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            cancelled = asyncio.create_task(scheduler.run(unexpected, single=True))
            waiting = asyncio.create_task(scheduler.run(unexpected, single=False))
            await asyncio.sleep(0)
            cancelled.cancel()
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            with pytest.raises(asyncio.CancelledError):
                await active
            assert not waiting.done()
            scheduler.stop()
            with pytest.raises(SchedulerClosedError):
                await waiting
            with pytest.raises(SchedulerClosedError):
                await scheduler.run(unexpected, single=True)
        finally:
            release.set()
