# meiva:note
# A pool whose connection acquisition parks until the watcher is cancelled, then does what the
# test says. No real Postgres wait misbehaves on cancel (psycopg unwinds a cancelled acquire or
# query at once), so the faults an exit has to survive — a watcher that raises while unwinding,
# stays wedged past the bound, or swallows the cancel — are produced where the watcher meets its
# resource instead of in a coroutine handed to the owner.
# meiva:link agent/graph/docs/interrupt-ownership.meiva.okf.md

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


# meiva:note
# `parked` is set once the watcher waits and `cancelled` once the cancel reaches it. The cancel
# then waits for `release` (set it from the start for a prompt unwind). It ends by raising
# `after_cancel`, or, with a `real` pool, by swallowing the cancel and polling that pool; with
# neither it propagates the cancel.
class ParkedPool(AsyncConnectionPool[AsyncConnection]):
    def __init__(
        self,
        *,
        release: asyncio.Event,
        after_cancel: BaseException | None = None,
        real: AsyncConnectionPool[AsyncConnection] | None = None,
    ) -> None:
        super().__init__("dbname=parked", open=False)
        self.parked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = release
        self.after_cancel = after_cancel
        self.real = real

    @asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncGenerator[AsyncConnection]:
        self.parked.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.release.wait()
            if self.after_cancel is not None:
                raise self.after_cancel from None
            if self.real is None:
                raise
        assert self.real is not None
        async with self.real.connection() as conn:
            yield conn
