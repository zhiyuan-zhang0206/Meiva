# meiva:note
# The live-view events a graph node published, read back the way the gateway reads them: a real
# `AgentEventPublisher` on the test Redis and a real subscriber on its channel. A test hands
# `live.publisher` to the code under test and asserts the payloads that arrived, in publish order.
# meiva:link agent/graph/docs/node-lifecycle.meiva.okf.md

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest_asyncio
from redis.asyncio.client import PubSub

from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER
from base.events.live.publisher import AgentEventPublisher

_ARRIVAL_TIMEOUT_S = 5.0


class LiveEvents:
    def __init__(self, publisher: AgentEventPublisher, subscription: PubSub) -> None:
        self.publisher = publisher
        self._subscription = subscription

    async def payloads(self) -> list[str]:
        # meiva:note
        # Everything published since the previous call. A marker emitted behind the events
        # travels the same FIFO channel, so its arrival proves every earlier event has arrived
        # too. The redis stubs leave the subscription's types unknown, so the message is cast.
        marker = f"flush-{uuid.uuid4()}"
        self.publisher.emit(marker)
        arrived: list[str] = []
        while True:
            raw = await self._subscription.get_message(  # pyright: ignore[reportUnknownMemberType]
                ignore_subscribe_messages=True, timeout=_ARRIVAL_TIMEOUT_S
            )
            message = cast("dict[str, Any] | None", raw)
            if message is None:
                raise AssertionError(f"live events did not arrive within {_ARRIVAL_TIMEOUT_S}s")
            data = str(message["data"])
            if data == marker:
                return arrived
            arrived.append(data)

    async def events(self) -> list[dict[str, Any]]:
        return [json.loads(payload) for payload in await self.payloads()]

    async def roles(self) -> list[str]:
        return [event["role"] for event in await self.events()]

    async def typed(self) -> list[Any]:
        return [EVENT_ADAPTER.validate_json(payload) for payload in await self.payloads()]


@pytest_asyncio.fixture
async def live_events(event_bus: EventBus) -> AsyncIterator[LiveEvents]:
    # meiva:note
    # The publisher runs under a task group the fixture enters itself, as a hosted service does,
    # and the subscription is established before the first event can be published. The redis
    # stubs leave the pubsub types unknown.
    subscriber = event_bus.open_async_redis()
    sender = event_bus.open_async_redis()
    tasks = asyncio.TaskGroup()
    await tasks.__aenter__()
    publisher = AgentEventPublisher(sender, event_bus.channel, agent_id=0)
    try:
        async with subscriber.pubsub() as subscription:  # pyright: ignore[reportUnknownMemberType]
            await subscription.subscribe(event_bus.channel)
            await subscription.get_message(timeout=_ARRIVAL_TIMEOUT_S)  # pyright: ignore[reportUnknownMemberType]
            await publisher.start(tasks)
            yield LiveEvents(publisher, subscription)
    finally:
        await publisher.aclose()
        await tasks.__aexit__(None, None, None)
        await sender.aclose()
        await subscriber.aclose()
