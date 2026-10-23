# meiva:note
# The live-view events an exec published, read back the way the gateway reads them: a real
# `AgentEventPublisher` on the test Redis and a real subscriber on a channel of its own. A test
# hands `capture.publisher` to the code under test and asserts the events that arrived, in
# publish order.
# meiva:link agent/graph/exec/docs/subprocess-tests.meiva.okf.md

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest_asyncio
import redis.asyncio as aredis
from redis.asyncio.client import PubSub

from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher

_ARRIVAL_TIMEOUT_S = 5.0


class EventCapture:
    def __init__(self, publisher: AgentEventPublisher, subscription: PubSub) -> None:
        self.publisher = publisher
        self._subscription = subscription

    async def events(self) -> list[dict[str, Any]]:
        # meiva:note
        # Everything published since the previous call. A marker emitted behind the events
        # travels the same FIFO channel, so its arrival proves every earlier event has arrived
        # too; redis-py's pub/sub stubs are untyped, hence the ignored member types below.
        marker = f"flush-{uuid.uuid4()}"
        self.publisher.emit(marker)
        arrived: list[dict[str, Any]] = []
        while True:
            message = cast(
                "dict[str, Any] | None",
                await self._subscription.get_message(  # pyright: ignore[reportUnknownMemberType]
                    ignore_subscribe_messages=True, timeout=_ARRIVAL_TIMEOUT_S
                ),
            )
            if message is None:
                raise AssertionError(f"live events did not arrive within {_ARRIVAL_TIMEOUT_S}s")
            data = message["data"]
            assert isinstance(data, str)
            if data == marker:
                return arrived
            arrived.append(json.loads(data))


@pytest_asyncio.fixture
async def event_capture(event_bus: EventBus) -> AsyncIterator[EventCapture]:
    # meiva:note
    # The capture's channel is private to the test, so no other publisher's events mix in.
    channel = f"exec-test-events:{uuid.uuid4().hex}"
    subscriber = event_bus.open_async_redis()
    sender = event_bus.open_async_redis()
    tasks = asyncio.TaskGroup()
    await tasks.__aenter__()
    publisher = AgentEventPublisher(sender, channel, agent_id=0)
    try:
        async with subscriber.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType]
            await subscription.subscribe(channel)
            await subscription.get_message(timeout=_ARRIVAL_TIMEOUT_S)  # pyright: ignore[reportUnknownMemberType]
            await publisher.start(tasks)
            yield EventCapture(publisher, subscription)
    finally:
        await publisher.aclose()
        await tasks.__aexit__(None, None, None)
        await sender.aclose()
        await subscriber.aclose()


# meiva:note
# A real publisher nobody started: its emits wait in the queue and nothing reaches Redis, for
# tests whose subject is not the events the exec publishes.
def idle_publisher() -> AgentEventPublisher:
    return AgentEventPublisher(aredis.Redis(), "exec-test-events:unused", agent_id=0)
