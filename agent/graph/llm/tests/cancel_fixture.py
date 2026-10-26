from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agent.graph.interrupt import InterruptEvent
from base.agents.messages.inbound import InterruptReason


class _CancelCursor:
    def __init__(self, event: InterruptEvent) -> None:
        self._event = event

    async def execute(self, _query: str, _params: object = None) -> None:
        return None

    async def fetchone(self) -> tuple[str, str]:
        await self._event.wait()
        if self._event.reason is InterruptReason.RESTART:
            return ("restart", "user")
        if self._event.reason is InterruptReason.SYSTEM:
            return ("cancel", "agent:0")
        return ("cancel", "user")


class _CancelConnection:
    def __init__(self, event: InterruptEvent) -> None:
        self._event = event

    @asynccontextmanager
    async def cursor(self) -> AsyncGenerator[_CancelCursor]:
        yield _CancelCursor(self._event)


# meiva:note
# In-memory interrupt pool that yields a pending cancel row once `event` is set.
class CancelPool:
    def __init__(self, event: InterruptEvent) -> None:
        self.event = event

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[_CancelConnection]:
        yield _CancelConnection(self.event)
