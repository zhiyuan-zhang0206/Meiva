from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.live.tests.fakes import async_redis_factory
from services.agent_runner.agent_host.dispatcher import (
    TurnScheduler,
)


class Recorder:
    def __init__(self) -> None:
        self.started: list[int] = []
        self.finished: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}
        self.entered: dict[int, asyncio.Event] = {}

    def gate(self, agent_id: int) -> asyncio.Event:
        return self.gates.setdefault(agent_id, asyncio.Event())

    def arrival(self, agent_id: int) -> asyncio.Event:
        return self.entered.setdefault(agent_id, asyncio.Event())

    async def __call__(self, agent_id: int) -> None:
        self.started.append(agent_id)
        self.arrival(agent_id).set()
        gate = self.gates.get(agent_id)
        if gate is not None:
            await gate.wait()
            gate.clear()
        self.finished.append(agent_id)


async def settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


class StuckTurn:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self._released = False
        self._task: asyncio.Task[None] | None = None

    async def __call__(self, _agent_id: int) -> None:
        self._task = asyncio.current_task()
        self.entered.set()
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if self._released:
                    raise
                continue

    async def release(self) -> None:
        self._released = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def aclose_without_hanging(sched: TurnScheduler, stuck: StuckTurn) -> bool:
    closing = asyncio.create_task(sched.aclose())
    try:
        done, _ = await asyncio.wait([closing], timeout=5)
        return closing in done
    finally:
        await stuck.release()
        closing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await closing


@contextlib.asynccontextmanager
async def releasing_stuck_turn() -> AsyncGenerator[StuckTurn, None]:
    turn = StuckTurn()
    try:
        yield turn
    finally:
        await turn.release()


class QueueingPubSub:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.closed = False

    async def psubscribe(self, _pattern: str) -> None:
        return None

    async def get_message(self, *, timeout: float) -> dict[str, object] | None:
        try:
            return await asyncio.wait_for(self.messages.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def aclose(self) -> None:
        self.closed = True


class QueueingRedis:
    def __init__(self, pubsub: QueueingPubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self, **_kwargs: object) -> QueueingPubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


# meiva:note
# The bus's async client opener: each open is a new queueing client, and the test keeps every one
# it handed out so it can assert how many subscriptions the dispatcher opened.
class QueueingRedisOpener:
    def __init__(self, pubsub: QueueingPubSub) -> None:
        self.clients: list[QueueingRedis] = []
        self._pubsub = pubsub

    def __call__(self) -> QueueingRedis:
        client = QueueingRedis(self._pubsub)
        self.clients.append(client)
        return client


# meiva:note
# A bus on the live settings whose async client comes from `opener`: the dispatcher's pub/sub
# reads reach the queueing stand-in, and nothing else of the bus is steered.
def redis_bus(opener: Callable[[], object]) -> EventBus:
    return EventBus(
        event_bus_config_from_settings(),
        rng=seeded_rng(),
        timers=SYSTEM_TIMERS,
        open_async_redis=async_redis_factory(opener),
    )
