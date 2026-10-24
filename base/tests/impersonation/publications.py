from __future__ import annotations

import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

from redis.client import PubSub

from base.cluster import inbound_channel
from base.events.live.bus import EventBus
from base.events.live.projection import EVENT_ADAPTER, Event

__all__ = ["NOTHING", "PublicationTap", "Published", "tap_publications"]

_ARRIVAL_TIMEOUT_S = 5.0


# meiva:note
# What one stretch of lease code published: the wakes that reached the tapped agents'
# inbound channels, as (agent id, payload), and the typed live events on the bus's events
# channel, each in publish order.
@dataclass(frozen=True)
class Published:
    wakes: tuple[tuple[int, str], ...]
    events: tuple[Event, ...]


NOTHING = Published(wakes=(), events=())


# meiva:note
# The reading half of a tap. Publications wait in the subscription until `drain()`. A marker
# published on a private channel behind them proves every earlier publication has arrived,
# because one subscriber connection receives in the order Redis processed the publishes, so
# "nothing was published" is a finished read and not a timeout.
class PublicationTap:
    def __init__(
        self,
        bus: EventBus,
        subscription: PubSub,
        *,
        flush_channel: str,
        wake_channels: dict[str, int],
    ) -> None:
        self._bus = bus
        self._subscription = subscription
        self._flush_channel = flush_channel
        self._wake_channels = wake_channels

    def drain(self) -> Published:
        marker = uuid.uuid4().hex
        delivered = self._bus.publish_best_effort_sync(marker, channel=self._flush_channel)
        assert delivered == 1, "the tap's own subscription did not receive its flush marker"
        wakes: list[tuple[int, str]] = []
        events: list[Event] = []
        while True:
            message = cast(
                "dict[str, Any] | None",
                self._subscription.get_message(
                    ignore_subscribe_messages=True, timeout=_ARRIVAL_TIMEOUT_S
                ),
            )
            if message is None:
                raise AssertionError(f"publications did not arrive within {_ARRIVAL_TIMEOUT_S}s")
            channel = str(message["channel"])
            data = str(message["data"])
            if channel == self._flush_channel:
                assert data == marker
                return Published(wakes=tuple(wakes), events=tuple(events))
            if channel == self._bus.channel:
                events.append(EVENT_ADAPTER.validate_json(data))
            else:
                wakes.append((self._wake_channels[channel], data))


# meiva:note
# Subscribe to the bus's events channel and to each agent's inbound wake channel for the
# block. The subscription is confirmed before the block starts, so nothing published inside
# it can be missed.
@contextmanager
def tap_publications(bus: EventBus, *agent_ids: int) -> Generator[PublicationTap]:
    if not agent_ids:
        raise ValueError("A publication tap needs at least one agent to listen for")
    wake_channels = {inbound_channel(agent_id, bus.channel): agent_id for agent_id in agent_ids}
    flush_channel = f"{bus.channel}:tap-flush:{uuid.uuid4().hex}"
    channels = [bus.channel, flush_channel, *wake_channels]
    with (
        bus.sync_redis(decode_responses=True) as redis,
        PubSub(redis.connection_pool) as subscription,
    ):
        subscription.subscribe(*channels)
        confirmed = 0
        deadline = time.monotonic() + _ARRIVAL_TIMEOUT_S
        while confirmed < len(channels):
            if time.monotonic() >= deadline:
                raise AssertionError("the publication subscription was not confirmed")
            message = cast("dict[str, Any] | None", subscription.get_message(timeout=0.5))
            if message is not None and message["type"] == "subscribe":
                confirmed += 1
        yield PublicationTap(
            bus, subscription, flush_channel=flush_channel, wake_channels=wake_channels
        )
