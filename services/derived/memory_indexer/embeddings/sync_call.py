# meiva:note
# Run a synchronous embed without extending its deadline to wait for stalled DNS.

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, override


class _DetachedExecutor(ThreadPoolExecutor):
    @override
    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        super().shutdown(wait=False, cancel_futures=cancel_futures)


def _attempt_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    loop.set_default_executor(_DetachedExecutor())
    return loop


def run_sync[T](operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
    # meiva:note
    # Run an async HTTP operation from a thread without a running event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation(), loop_factory=_attempt_loop)
    raise RuntimeError(
        "This is the sync embedding provider API; call it from a worker thread "
        "with asyncio.to_thread or an executor. From async code use "
        "embed_query_async where available; batch embedding stays sync "
        "and the indexer calls it via asyncio.to_thread."
    )
