"""`meiva/sdk_surface/batch.py` unit tests — the one concurrent batch executor.

The public SDK entry points (search / fetch / understand) each have their own
integration tests for the batch behaviour (order, concurrency cap, error
messages); these are the direct unit tests of the shared executor and its
`max_concurrent` contract.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from meiva.sdk_surface.batch import run_batch

# ─── run_batch ────────────────────────────────────────────────────────────


def test_run_batch_preserves_input_order() -> None:
    """Results come back in input order regardless of which worker finishes
    first."""

    def worker(n: int) -> int:
        time.sleep((10 - n) / 100)  # later items finish first
        return n * 2

    assert asyncio.run(run_batch([1, 2, 3], worker, None)) == [2, 4, 6]


def test_run_batch_empty_list() -> None:
    assert asyncio.run(run_batch([], lambda x: x, None)) == []  # pyright: ignore[reportUnknownArgumentType]


def test_run_batch_caps_inflight_with_semaphore() -> None:
    """max_concurrent=N keeps at most N workers in flight — the observed peak
    never exceeds N (the semaphore is the shared copy of the three old
    `_*_all` implementations)."""
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def worker(_: int) -> int:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        return 1

    asyncio.run(run_batch(list(range(8)), worker, 3))
    assert peak <= 3
    assert peak >= 2  # actually ran concurrently, not serialized


def test_run_batch_propagates_first_error() -> None:
    """A failing worker propagates its exception out of the batch — callers
    see the failure, not a partial result list."""

    def worker(n: int) -> int:
        if n == 2:
            raise LookupError("boom")
        return n

    with pytest.raises(LookupError, match="boom"):
        asyncio.run(run_batch([1, 2, 3], worker, None))
