# meiva:note
# The listener's channel prefix and its wait budget are the values it is handed: a record that
# answered "which channel" or "how much time is left" from the process it ran in could not be told
# either, so these two facts arrive as arguments and a `ManualTimers` decides when a budget ends.

from __future__ import annotations

import asyncio
from typing import Any, cast

from redis.asyncio.client import PubSub as _RedisPubSub

from base.clock.tests.manual_timers import ManualTimers
from base.cluster import inbound_channel
from base.events.live.redis_listener import RedisInboundListener


# meiva:note
# A pubsub double that spends the timeout it is asked for on the manual clock, which is how one
# consume iteration can run out a real budget without waiting on the wall clock.
class TimedPubSub:
    def __init__(self, timers: ManualTimers) -> None:
        self._timers = timers
        self.asked: list[float] = []

    async def get_message(self, *, timeout: float, ignore_subscribe_messages: bool = True) -> None:
        self.asked.append(timeout)
        self._timers.advance(timeout)


def test_the_channel_prefix_is_the_value_handed_in() -> None:
    listener = RedisInboundListener(
        "redis://unused",
        7121,
        events_channel="meiva:other:events",
        timers=ManualTimers(),
    )

    assert listener._channel == inbound_channel(7121, "meiva:other:events")


# meiva:note
# One consume spends the whole budget on the manual clock and returns at once. A deadline read from
# the process clock would never expire here: the manual clock only moves when the double is asked
# for a timeout, so the second reading is the first plus the budget.
async def test_the_wait_deadline_is_read_from_the_timers_it_is_handed() -> None:
    timers = ManualTimers()
    listener = RedisInboundListener(
        "redis://unused",
        7122,
        events_channel="meiva:tests:events",
        timers=timers,
    )
    pubsub = TimedPubSub(timers)

    await asyncio.wait_for(
        listener._consume_one(cast("_RedisPubSub", cast(Any, pubsub)), 5.0), timeout=2.0
    )

    assert pubsub.asked == [5.0]


# meiva:note
# A listener whose connection cannot be opened backs off on the timers it was handed: the manual
# clock records the wait and moves past it, so the loop spends its budget without touching the wall
# clock. A backoff on `asyncio.sleep` would move nothing, never end the budget, and hang here.
async def test_a_lost_connection_backs_off_on_the_timers_it_is_handed() -> None:
    timers = ManualTimers()

    def _refuse(*args: object, **kwargs: object) -> object:
        raise OSError("no route to the cluster redis")

    listener = RedisInboundListener(
        "redis://unused",
        7123,
        events_channel="meiva:tests:events",
        timers=timers,
        redis_factory=_refuse,
    )

    await asyncio.wait_for(listener.wait_one(3.0), timeout=10.0)

    assert timers.sleeps
    assert sum(timers.sleeps) <= 3.0
