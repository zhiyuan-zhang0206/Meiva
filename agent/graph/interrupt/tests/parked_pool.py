# meiva:note
# A stand-in for the one pool member the interrupt watcher calls, `connection()`: acquisition
# parks until the watcher is cancelled, then does what the constructor said. No real Postgres wait
# misbehaves on cancel (psycopg unwinds a cancelled acquire or query at once), so the faults an
# exit has to survive — a watcher that raises while unwinding, stays wedged past the bound, or
# swallows the cancel — are produced where the watcher meets its resource instead of in a coroutine
# handed to the owner. It is not a subclass of the psycopg pool and carries no switch: what it does
# is fixed at construction, and `parked` / `cancelled` only report where the watcher is.
# Pending a base port: base/ is to host a narrow `InterruptPool` protocol and this fake. Then this
# file is deleted and `subscribe_interrupt` types its pool with the protocol, so `as_pool` and its
# cast go with it.
# meiva:link agent/graph/docs/interrupt-ownership.meiva.okf.md

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


# meiva:note
# `parked` is set once the watcher waits and `cancelled` once the cancel reaches it. The cancel
# then waits for `release` (set it from the start for a prompt unwind). It ends by raising
# `after_cancel`, or, with a `real` pool, by swallowing the cancel and polling that pool; with
# neither it propagates the cancel. Giving both is refused: the two endings exclude each other.
class ParkedPool:
    def __init__(
        self,
        *,
        release: asyncio.Event,
        after_cancel: BaseException | None = None,
        real: AsyncConnectionPool[AsyncConnection] | None = None,
    ) -> None:
        if after_cancel is not None and real is not None:
            raise ValueError("a parked pool either raises after the cancel or swallows it")
        self.parked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._release = release
        self._after_cancel = after_cancel
        self._real = real

    # meiva:note
    # The watcher is typed against the concrete psycopg pool until base hosts the narrow port;
    # this is the only place the stand-in is presented as one.
    def as_pool(self) -> AsyncConnectionPool[AsyncConnection]:
        return cast(AsyncConnectionPool[AsyncConnection], self)

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection]:
        self.parked.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self._release.wait()
            if self._after_cancel is not None:
                raise self._after_cancel from None
            if self._real is None:
                raise
        assert self._real is not None
        async with self._real.connection() as conn:
            yield conn
