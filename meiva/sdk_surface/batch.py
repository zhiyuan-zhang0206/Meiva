# meiva:note
# The single concurrent batch executor behind the SDK batch APIs. `meiva.web.search` /
# `meiva.web.fetch` / `meiva.understand` share one batch pattern: validate items, validate knobs,
# then schedule every worker concurrently with order preserved and the first error propagated.
# `run_batch` is the one place the semaphore scheduling exists; the `max_concurrent` knob's
# contract is `pure/plans/batch_plan.py`.

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable

from meiva.sdk_surface.pure.plans.batch_plan import validate_max_concurrent


# meiva:note
# Run `worker` over every item concurrently.
#
# Order is preserved; the first error propagates. `max_concurrent` caps how many workers are in
# flight at once via a semaphore; None keeps concurrency unbounded — a provider 429 propagates
# like any other upstream failure. Per-result bookkeeping (e.g. `meiva.understand`'s auto-save)
# belongs to the caller, which runs it over `zip(items, results)` once the batch returned.
async def run_batch[T, R](
    items: list[T],
    worker: Callable[[T], R],
    max_concurrent: int | None,
) -> list[R]:
    validate_max_concurrent(max_concurrent, example="<batch api>(..., max_concurrent=N)")
    if max_concurrent is None:
        tasks = [asyncio.to_thread(worker, item) for item in items]
    else:
        sem = threading.Semaphore(max_concurrent)

        def _bounded(item: T) -> R:
            with sem:
                return worker(item)

        tasks = [asyncio.to_thread(_bounded, item) for item in items]
    return await asyncio.gather(*tasks)
