from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine

from base.lm.harness.antigravity.transport import AgyTransport
from base.lm.harness.session import SessionClock
from base.log import logger


class TimingRefresh:
    def __init__(
        self,
        clock: SessionClock,
        spawn: Callable[[Coroutine[object, object, None]], asyncio.Task[None]],
        *,
        agent_id: int | None,
        interval: float,
    ) -> None:
        self._clock = clock
        self._spawn = spawn
        self._agent_id = agent_id
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._pending = False
        self._last: float | None = None

    def request(self, transport: AgyTransport, active: Callable[[], bool]) -> None:
        if not transport.has_timing_record():
            return
        self._pending = True
        if self._task is None or self._task.done():
            self._task = self._spawn(self._run(transport, active))

    def cancel(self) -> None:
        self._pending = False
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            _ = task.cancel()

    def _wait_seconds(self) -> float:
        return (
            0.0
            if self._last is None
            else max(0.0, self._interval - (self._clock.monotonic() - self._last))
        )

    async def _run(self, transport: AgyTransport, active: Callable[[], bool]) -> None:
        try:
            while self._pending and active():
                wait = self._wait_seconds()
                if wait > 0.0:
                    await self._clock.asleep(wait)
                    if not active():
                        return
                self._pending = False
                self._last = self._clock.monotonic()
                if not await self._collect(transport, active):
                    return
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    async def _collect(self, transport: AgyTransport, active: Callable[[], bool]) -> bool:
        try:
            _ = await transport.trajectory()
        except Exception as exc:
            if active():
                logger.warning(
                    "agent {agent_id}: background timing refresh failed ({error}); "
                    "provider execution is unchanged",
                    event="harness_timing_collection_failed",
                    phase="refresh",
                    agent_id=self._agent_id,
                    error=type(exc).__name__,
                )
            return False
        return True
