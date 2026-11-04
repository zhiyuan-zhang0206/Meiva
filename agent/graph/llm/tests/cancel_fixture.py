from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agent.graph.interrupt import InterruptEvent
from base.agents.messages.inbound import InterruptReason
from base.db.tests.row_fakes import FakeAsyncConnection, RowFactory


class _CancelCursor:
    def __init__(self, event: InterruptEvent, row_factory: RowFactory) -> None:
        self._event = event
        self._row_factory = row_factory

    async def execute(self, _query: str, _params: object = None) -> None:
        return None

    async def fetchone(self) -> object:
        await self._event.wait()
        if self._event.reason is InterruptReason.RESTART:
            row = ("restart", "user")
        elif self._event.reason is InterruptReason.SYSTEM:
            row = ("cancel", "agent:0")
        else:
            row = ("cancel", "user")
        async with FakeAsyncConnection(("kind", "source"), [row]).cursor(
            row_factory=self._row_factory
        ) as cursor:
            return await cursor.fetchone()


class _CancelConnection:
    def __init__(self, event: InterruptEvent) -> None:
        self._event = event

    @asynccontextmanager
    async def cursor(self, *, row_factory: RowFactory) -> AsyncGenerator[_CancelCursor]:
        yield _CancelCursor(self._event, row_factory)


# meiva:note
# In-memory interrupt pool that yields a pending cancel row once `event` is set.
class CancelPool:
    def __init__(self, event: InterruptEvent) -> None:
        self.event = event

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[_CancelConnection]:
        yield _CancelConnection(self.event)
