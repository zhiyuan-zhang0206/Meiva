from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg

from base.db.transaction import AsyncConnectionSource


# meiva:note
# An async pool whose first borrow waits at the pool and loses one cancellation of that wait.
#
# A real pool never swallows a `CancelledError`; this one does it once, which is the
# cancel-versus-completion race the interrupt watcher must survive. The first caller is blocked
# inside `connection()` until `release()`; its first cancellation is absorbed (`swallowed` observes
# it) and the wait resumes, a second one propagates. `waiting` is set once the first caller is
# waiting, so a test sequences against the real state instead of sleeping. Every later borrow goes
# straight to the wrapped pool. A borrow that merely has to wait is not this fault: occupy the
# real pool's connections instead.
class CancelSwallowingPool:
    def __init__(self, pool: AsyncConnectionSource) -> None:
        self._pool = pool
        self._released = asyncio.Event()
        self._first_taken = False
        self.waiting = asyncio.Event()
        self.swallowed = asyncio.Event()

    def release(self) -> None:
        self._released.set()

    @asynccontextmanager
    async def connection(
        self, timeout: float | None = None
    ) -> AsyncGenerator[psycopg.AsyncConnection[Any]]:
        if not self._first_taken:
            self._first_taken = True
            self.waiting.set()
            await self._wait_for_release()
        async with self._pool.connection(timeout=timeout) as conn:
            yield conn

    async def _wait_for_release(self) -> None:
        while not self._released.is_set():
            try:
                await self._released.wait()
            except asyncio.CancelledError:
                if self.swallowed.is_set():
                    raise
                self.swallowed.set()
