# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

type Vectors = list[list[float]]
type Queue = OrderedDict[asyncio.Future[Vectors], Callable[[], Vectors]]


class SchedulerClosedError(Exception): ...


class InferenceScheduler:
    def __init__(self, executor: ThreadPoolExecutor) -> None:
        self._executor = executor
        self._singles: Queue = OrderedDict()
        self._batches: Queue = OrderedDict()
        self._batch_due = False
        self._active = False
        self._stopped = False

    async def run(self, infer: Callable[[], Vectors], *, single: bool) -> Vectors:
        if self._stopped:
            raise SchedulerClosedError
        result: asyncio.Future[Vectors] = asyncio.get_running_loop().create_future()
        queue = self._singles if single else self._batches
        queue[result] = infer
        try:
            self._dispatch()
            return await result
        finally:
            _ = queue.pop(result, None)
            if not self._batches:
                self._batch_due = False

    def _dispatch(self) -> None:
        if self._stopped or self._active:
            return
        for queue in (self._singles, self._batches):
            while queue and next(iter(queue)).cancelled():
                _ = queue.popitem(last=False)
        if not self._batches:
            self._batch_due = False
        if self._singles and not self._batch_due:
            result, infer = self._singles.popitem(last=False)
            self._batch_due = bool(self._batches)
        elif self._batches:
            result, infer = self._batches.popitem(last=False)
            self._batch_due = False
        else:
            return

        self._active = True
        work = asyncio.get_running_loop().run_in_executor(self._executor, infer)
        work.add_done_callback(partial(self._complete, result))

    def _complete(self, result: asyncio.Future[Vectors], work: asyncio.Future[Vectors]) -> None:
        error = work.exception()
        if not result.cancelled():
            if error is None:
                result.set_result(work.result())
            else:
                result.set_exception(error)
        self._active = False
        self._dispatch()

    def stop(self) -> None:
        self._stopped = True
        for queue in (self._singles, self._batches):
            for result in queue:
                if not result.cancelled():
                    result.set_exception(SchedulerClosedError())
            queue.clear()
