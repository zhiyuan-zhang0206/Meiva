import asyncio

import pytest
from psycopg_pool import AsyncConnectionPool

from base.db.tests.swallowed_cancel import CancelSwallowingPool


async def test_the_first_borrow_waits_for_release_and_is_then_lent_a_real_connection(
    aops_pool: AsyncConnectionPool,
) -> None:
    pool = CancelSwallowingPool(aops_pool)

    async def borrow() -> int:
        async with pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            assert row is not None
            return int(row[0])

    first = asyncio.create_task(borrow())
    try:
        await asyncio.wait_for(pool.waiting.wait(), 2)
        assert not first.done()
        pool.release()
        assert await asyncio.wait_for(first, 2) == 1
        assert await asyncio.wait_for(borrow(), 2) == 1
    finally:
        pool.release()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_only_the_first_cancellation_of_the_wait_is_swallowed(
    aops_pool: AsyncConnectionPool,
) -> None:
    pool = CancelSwallowingPool(aops_pool)
    borrower = asyncio.create_task(pool.connection().__aenter__())
    try:
        await asyncio.wait_for(pool.waiting.wait(), 2)
        borrower.cancel()
        await asyncio.wait_for(pool.swallowed.wait(), 2)
        assert not borrower.done()
        borrower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(borrower, 2)
        assert pool.swallowed.is_set()
    finally:
        pool.release()
        borrower.cancel()
        await asyncio.gather(borrower, return_exceptions=True)
