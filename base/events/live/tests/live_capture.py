# meiva:note
# The live-view events a test published, read back the way the gateway reads them: a real
# `AgentEventPublisher` on the test Redis and a real subscriber on a channel of the test's own. A
# test hands `live_events.publisher` to the code under test and asserts the events that arrived, in
# publish order. `recorded_events` / `snapshots_at_events` are the synchronous form for code that
# publishes through the bus itself.

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import contextmanager
from typing import Any, cast

import pytest_asyncio
import redis.asyncio as aredis
from redis.asyncio.client import PubSub

from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER
from base.events.live.publisher import AgentEventPublisher

__all__ = [
    "LiveEvents",
    "idle_publisher",
    "live_events",
    "recorded_events",
    "snapshots_at_events",
]

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
    # The channel is private to the test, so no other publisher's events mix in. The publisher
    # runs under a task group the fixture enters itself, as a hosted service does, and the
    # subscription is established before the first event can be published. The redis stubs leave
    # the pubsub types unknown.
    channel = f"live-test-events:{uuid.uuid4().hex}"
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
            yield LiveEvents(publisher, subscription)
    finally:
        await publisher.aclose()
        await tasks.__aexit__(None, None, None)
        await sender.aclose()
        await subscriber.aclose()


# meiva:note
# A real publisher nobody started: its emits wait in the queue and nothing reaches Redis, for
# tests whose subject is not the events the code under test publishes.
def idle_publisher() -> AgentEventPublisher:
    return AgentEventPublisher(aredis.Redis(), "live-test-events:unused", agent_id=0)


# meiva:note
# The live events a synchronous operation published, read back from the cluster Redis the test
# provisions: a subscription opened before the operation collects what arrived on the events
# channel by the time the block ends.
@contextmanager
def recorded_events(bus: EventBus) -> Generator[list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    client = bus.sync_redis(decode_responses=True)
    subscription: Any = cast("Any", client).pubsub()
    try:
        subscription.subscribe(bus.channel)
        confirmation = subscription.get_message(timeout=2.0)
        assert confirmation is not None and confirmation["type"] == "subscribe"
        yield received
        while (message := subscription.get_message(timeout=0.5)) is not None:
            if message["type"] == "message":
                received.append(json.loads(str(message["data"])))
    finally:
        subscription.close()
        client.close()


# meiva:note
# Each live event published while the block runs, with what `snapshot()` read the moment the event
# arrived: a hint that names committed work must find the work visible to a separate connection.
@contextmanager
def snapshots_at_events[T](
    bus: EventBus, snapshot: Callable[[], T]
) -> Generator[list[tuple[dict[str, Any], T]]]:
    seen: list[tuple[dict[str, Any], T]] = []
    client = bus.sync_redis(decode_responses=True)
    subscription: Any = cast("Any", client).pubsub()
    subscription.subscribe(bus.channel)
    confirmation = subscription.get_message(timeout=2.0)
    assert confirmation is not None and confirmation["type"] == "subscribe"
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            message = subscription.get_message(timeout=0.05)
            if message is not None and message["type"] == "message":
                seen.append((json.loads(str(message["data"])), snapshot()))

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        yield seen
        time.sleep(0.3)
    finally:
        stop.set()
        watcher.join()
        subscription.close()
        client.close()
