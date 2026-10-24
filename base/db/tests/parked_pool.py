from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import psycopg

from base.db.transaction import AsyncConnectionSource


# meiva:note
# An async pool whose `park_on`-th borrow stops at the pool and waits there.
#
# The borrow is lent to the caller only after `release()`; until then the caller is blocked inside
# `connection()` at its cancellation point. With `swallow_cancel` the first cancellation of that
# wait is absorbed and the wait resumes, which is how a pool loses a `CancelledError` in the
# cancel-versus-completion race. Every other borrow goes straight through to the wrapped pool.
# `parked` is set once a caller is waiting, so a test sequences against the real state instead of
# sleeping.
class ParkedAsyncPool:
    def __init__(
        self, pool: AsyncConnectionSource, *, park_on: int, swallow_cancel: bool = False
    ) -> None:
        if park_on < 1:
            raise ValueError("park_on counts borrows from 1")
        self._pool = pool
        self._park_on = park_on
        self._swallow_cancel = swallow_cancel
        self._released = asyncio.Event()
        self.parked = asyncio.Event()
        self.borrows = 0
        self.cancellations_swallowed = 0

    def release(self) -> None:
        self._released.set()

    @asynccontextmanager
    async def connection(
        self, timeout: float | None = None
    ) -> AsyncGenerator[psycopg.AsyncConnection[Any]]:
        self.borrows += 1
        if self.borrows == self._park_on:
            self.parked.set()
            await self._wait_for_release()
        async with self._pool.connection(timeout=timeout) as conn:
            yield conn

    async def _wait_for_release(self) -> None:
        while not self._released.is_set():
            try:
                await self._released.wait()
            except asyncio.CancelledError:
                if not self._swallow_cancel or self.cancellations_swallowed:
                    raise
                self.cancellations_swallowed += 1
