"""The live event-bus Redis helper reads the configured URL and forwards options."""

from __future__ import annotations

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus


def test_sync_redis_ping(redis_url: str) -> None:
    client = EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS).sync_redis()
    try:
        assert client.ping() is True  # pyright: ignore[reportUnknownMemberType]
    finally:
        client.close()


def test_sync_redis_decode_responses_passthrough(redis_url: str) -> None:
    client = EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS).sync_redis(
        decode_responses=True
    )
    try:
        client.set("meiva:test:connect-helper", "v")
        assert client.get("meiva:test:connect-helper") == "v"  # str, not bytes
    finally:
        client.delete("meiva:test:connect-helper")
        client.close()
