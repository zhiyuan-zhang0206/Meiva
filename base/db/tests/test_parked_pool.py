import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import psycopg
import pytest

from base.db.tests.parked_pool import ParkedAsyncPool
from base.db.transaction import AsyncConnectionSource


# meiva:note
# A pool that lends a marker object and counts how many borrows reached it.
class _CountingPool:
    def __init__(self) -> None:
        self.lent = 0

    @asynccontextmanager
    async def connection(
        self, timeout: float | None = None
    ) -> AsyncGenerator[psycopg.AsyncConnection[Any]]:
        self.lent += 1
        yield cast(psycopg.AsyncConnection[Any], object())


async def test_only_the_named_borrow_waits_and_it_is_lent_after_release() -> None:
    inner = _CountingPool()
    pool = ParkedAsyncPool(cast(AsyncConnectionSource, inner), park_on=2)

    async with pool.connection():
        pass
    assert inner.lent == 1 and not pool.parked.is_set()

    async def second() -> None:
        async with pool.connection():
            pass

    borrower = asyncio.create_task(second())
    await asyncio.wait_for(pool.parked.wait(), 2)
    assert inner.lent == 1 and not borrower.done()
    pool.release()
    await asyncio.wait_for(borrower, 2)
    assert inner.lent == 2

    async with pool.connection():
        pass
    assert inner.lent == 3


async def test_a_parked_borrow_is_cancelled_unless_the_pool_swallows_it() -> None:
    plain = ParkedAsyncPool(cast(AsyncConnectionSource, _CountingPool()), park_on=1)
    borrower = asyncio.create_task(plain.connection().__aenter__())
    await asyncio.wait_for(plain.parked.wait(), 2)
    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await borrower

    lossy = ParkedAsyncPool(
        cast(AsyncConnectionSource, _CountingPool()), park_on=1, swallow_cancel=True
    )
    waiting = asyncio.create_task(lossy.connection().__aenter__())
    await asyncio.wait_for(lossy.parked.wait(), 2)
    waiting.cancel()
    while not lossy.cancellations_swallowed:
        await asyncio.sleep(0.01)
    assert not waiting.done()
    lossy.release()
    await asyncio.wait_for(waiting, 2)


def test_a_zero_park_index_is_refused() -> None:
    with pytest.raises(ValueError, match="from 1"):
        ParkedAsyncPool(cast(AsyncConnectionSource, _CountingPool()), park_on=0)
