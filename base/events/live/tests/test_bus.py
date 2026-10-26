"""`EventBus`: the handle binds one config to the transport; the shim publish and the handle's
share one body."""

from __future__ import annotations

import redis.asyncio as aredis

from base.clock.tests.seeded_rng import seeded_rng
from base.config import settings
from base.events.live.bus import EventBus, EventBusConfig


def _bus(url: str = "redis://secret@host:1/0", channel: str = "meiva:test") -> EventBus:
    return EventBus(EventBusConfig(redis_url=url, events_channel=channel), rng=seeded_rng())


def test_config_hides_the_url_from_repr() -> None:
    assert "secret" not in repr(EventBusConfig(redis_url="redis://secret@h/0", events_channel="c"))


def test_from_settings_reads_the_live_settings() -> None:
    bus = EventBus.from_settings(rng=seeded_rng())
    assert bus.channel == settings.data_plane.events_channel
    assert bus._config.redis_url == settings.data_plane.redis_url


async def test_the_async_client_is_shared_per_loop_and_dialed_with_the_handles_url() -> None:
    handle = _bus("redis://one:6379/0")
    first = handle.async_redis()
    second = handle.async_redis()
    other = _bus("redis://two:6379/0").async_redis()
    try:
        # meiva:note
        # redis.asyncio types connection_kwargs as an untyped dict on ConnectionPool.
        assert first is second
        assert first.connection_pool.connection_kwargs["host"] == "one"  # pyright: ignore[reportUnknownMemberType]
        assert other is not first
        assert other.connection_pool.connection_kwargs["host"] == "two"  # pyright: ignore[reportUnknownMemberType]
    finally:
        await first.aclose()
        await other.aclose()


async def test_publish_goes_to_the_events_channel_unless_another_is_named(
    redis_url: str,
) -> None:
    # meiva:note
    # redis.asyncio leaves from_url and pubsub partially untyped.
    handle = _bus(redis_url, channel="meiva:events:bus-test")
    subscriber = aredis.Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    pubsub = subscriber.pubsub()  # pyright: ignore[reportUnknownMemberType]
    await pubsub.subscribe("meiva:events:bus-test", "meiva:page:bus-test")
    await pubsub.get_message(timeout=1.0)
    await pubsub.get_message(timeout=1.0)
    try:
        assert await handle.publish_best_effort("p1", context="t") == 1
        assert await handle.publish_best_effort("p2", channel="meiva:page:bus-test") == 1
        msg1 = await pubsub.get_message(timeout=1.0, ignore_subscribe_messages=True)
        msg2 = await pubsub.get_message(timeout=1.0, ignore_subscribe_messages=True)
        assert msg1 is not None and (msg1["channel"], msg1["data"]) == (
            "meiva:events:bus-test",
            "p1",
        )
        assert msg2 is not None and (msg2["channel"], msg2["data"]) == (
            "meiva:page:bus-test",
            "p2",
        )
    finally:
        await pubsub.aclose()
        await subscriber.aclose()
        await handle.async_redis().aclose()


async def test_a_failed_publish_returns_none_and_never_raises(
    redis_access: None,
) -> None:
    del redis_access
    handle = _bus("redis://127.0.0.1:1/0")
    try:
        assert await handle.publish_best_effort("p") is None
    finally:
        await handle.async_redis().aclose()


def test_sync_publish_uses_a_one_off_client_and_closes_it(redis_url: str) -> None:
    handle = _bus(redis_url, "meiva:e:sync-test")
    assert handle.publish_best_effort_sync("p", context="t") == 0
