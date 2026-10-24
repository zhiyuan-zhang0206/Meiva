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
    await asyncio.wait_for(pool.waiting.wait(), 2)
    assert not first.done()
    pool.release()

    assert await asyncio.wait_for(first, 2) == 1
    assert await asyncio.wait_for(borrow(), 2) == 1


async def test_only_the_first_cancellation_of_the_wait_is_swallowed(
    aops_pool: AsyncConnectionPool,
) -> None:
    pool = CancelSwallowingPool(aops_pool)
    borrower = asyncio.create_task(pool.connection().__aenter__())
    await asyncio.wait_for(pool.waiting.wait(), 2)

    borrower.cancel()
    while not pool.swallowed:
        await asyncio.sleep(0.01)
    assert not borrower.done()

    borrower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(borrower, 2)
    assert pool.swallowed == 1
