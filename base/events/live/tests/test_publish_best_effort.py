"""Tests for the `publish_best_effort` / `publish_best_effort_sync` primitives.

Known Redis/network failures retain the bounded best-effort policy (NOPERM /
ResponseError -> WARNING, transient -> DEBUG). Programming errors propagate to
the calling owner; a committed DB row remains committed when its hint fails.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import DataError, NoPermissionError, ResponseError

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config import settings
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.live.redis_client import (
    AuthRetryAsyncRedis,
    AuthRetrySyncRedis,
    open_async_redis,
    open_sync_redis,
)
from base.events.live.redis_resilience import AuthRetryPolicy, should_warn_publish_failure
from base.events.live.tests.fakes import async_redis_factory, sync_redis_factory

_FAST_RETRY = AuthRetryPolicy(initial_delay_s=0.0005, delay_cap_s=0.002, window_s=1.0)


# meiva:note
# A bus on the fast retry policy whose client openers the test chooses; the defaults are the real
# ones, so a caller that names neither measures the production path.
def _bus_with(
    *,
    open_async: Callable[..., AuthRetryAsyncRedis] = open_async_redis,
    open_sync: Callable[..., AuthRetrySyncRedis] = open_sync_redis,
) -> EventBus:
    return EventBus(
        replace(event_bus_config_from_settings(), auth_retry_policy=_FAST_RETRY),
        rng=seeded_rng(),
        timers=SYSTEM_TIMERS,
        open_async_redis=open_async,
        open_sync_redis=open_sync,
    )


_bus = _bus_with()


@pytest.fixture(autouse=True)
def _skip_acl_backoff_waits() -> None:
    """Exercise the terminal best-effort contract without waiting 45.5 seconds.

    Retry cadence and its bound are asserted in test_redis_client; these tests
    own warning classification after retries have been exhausted.
    """
    global _bus  # noqa: PLW0603 — a bus built from the settings the session has by now
    _bus = _bus_with()


class _BoomSyncClient:
    """A sync redis stand-in whose publish raises — asserts the wrapper swallows."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def publish(self, _channel: str, _payload: str, *, auth_retry: bool = True) -> int:
        raise self._exc

    def close(self) -> None:  # closed in the wrapper's finally
        pass


class _BoomAsyncClient:
    """An async redis stand-in whose publish raises."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def publish(self, _channel: str, _payload: str, *, auth_retry: bool = True) -> int:
        raise self._exc


class TestPublishBestEffortSync:
    def test_returns_receiver_count_on_real_redis(self, event_bus: EventBus) -> None:
        """A successful publish returns the receiver count (0 with nobody subscribed)
        — an int, never None."""
        n = event_bus.publish_best_effort_sync("{}", context="test")
        assert n == 0

    def test_never_raises_and_debug_logs_on_transient(
        self,
        loguru_records: list[dict],
    ) -> None:
        """A transient failure (redis down) is swallowed → None, logged at DEBUG."""
        bus = _bus_with(
            open_sync=sync_redis_factory(lambda: _BoomSyncClient(RedisConnectionError("down")))
        )
        result = bus.publish_best_effort_sync("{}", channel="meiva:x", context="unit")
        assert result is None
        hits = [
            r for r in loguru_records if "skipped" in r["message"] and "meiva:x" in r["message"]
        ]
        assert hits, "expected a best-effort DEBUG skip line"
        assert all(r["level"].no < 30 for r in hits), "transient failure must be DEBUG, not WARNING"  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]

    def test_non_transport_failure_propagates(self) -> None:
        bus = _bus_with(
            open_sync=sync_redis_factory(lambda: _BoomSyncClient(AttributeError("bug")))
        )
        with pytest.raises(AttributeError, match="bug"):
            bus.publish_best_effort_sync("{}", channel="meiva:bug", context="unit")

    def test_never_raises_and_warns_on_noperm(
        self,
        loguru_records: list[dict],
    ) -> None:
        """A ResponseError (redis NOPERM — ACL misconfig) is swallowed → None, but
        logged at WARNING because it silently disables live updates fleet-wide."""
        bus = _bus_with(
            open_sync=sync_redis_factory(lambda: _BoomSyncClient(NoPermissionError("NOPERM")))
        )
        result = bus.publish_best_effort_sync("{}", channel="meiva:noperm-sync", context="unit")
        assert result is None
        assert any(
            "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for r in loguru_records
        ), "a NOPERM publish must be logged at WARNING"

    def test_noperm_warning_is_rate_limited(
        self,
        loguru_records: list[dict],
    ) -> None:
        """A persistent NOPERM outage warns once per channel then drops repeats to
        DEBUG — one event funnels the whole fleet through here, so the WARNING must
        not flood."""
        bus = _bus_with(
            open_sync=sync_redis_factory(lambda: _BoomSyncClient(NoPermissionError("NOPERM")))
        )
        channel = "meiva:noperm-throttle"
        for _ in range(4):
            assert bus.publish_best_effort_sync("{}", channel=channel, context="unit") is None
        warnings = [
            r
            for r in loguru_records
            if "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownMemberType]
        ]
        assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"  # pyright: ignore[reportUnknownArgumentType]
        assert any("rate-limited" in r["message"] and r["level"].no < 30 for r in loguru_records), (  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "suppressed repeats must drop to a DEBUG rate-limited line"
        )


class TestPublishBestEffortAsync:
    async def test_returns_receiver_count_on_real_redis(self, event_bus: EventBus) -> None:
        n = await event_bus.publish_best_effort("{}", context="test")
        assert n == 0

    async def test_never_raises_and_debug_logs_on_transient(
        self,
        loguru_records: list[dict],
    ) -> None:
        bus = _bus_with(
            open_async=async_redis_factory(lambda: _BoomAsyncClient(RedisConnectionError("down")))
        )
        result = await bus.publish_best_effort("{}", channel="meiva:x", context="unit")
        assert result is None
        assert any("skipped" in r["message"] and r["level"].no < 30 for r in loguru_records), (  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            "expected a best-effort DEBUG skip line"
        )

    async def test_never_raises_and_warns_on_noperm(
        self,
        loguru_records: list[dict],
    ) -> None:
        bus = _bus_with(
            open_async=async_redis_factory(lambda: _BoomAsyncClient(NoPermissionError("NOPERM")))
        )
        result = await bus.publish_best_effort("{}", channel="meiva:noperm-async", context="unit")
        assert result is None
        assert any(
            "rejected by redis" in r["message"] and r["level"].no >= 30  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            for r in loguru_records
        )

    async def test_reports_live_receiver_count(self, event_bus: EventBus) -> None:
        """With a real subscriber on the channel, the count is the number of
        receivers — the signal any 0-receiver guard relies on."""
        import redis.asyncio as aredis

        channel = f"{settings.data_plane.events_channel}:count"
        client = aredis.Redis.from_url(settings.data_plane.redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
        pubsub = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
        await pubsub.subscribe(channel)
        await pubsub.get_message(timeout=1.0)  # drain the subscribe confirmation
        try:
            n = await event_bus.publish_best_effort("{}", channel=channel, context="test")
            assert n == 1
        finally:
            await pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]
            await pubsub.aclose()
            await client.aclose()


def test_zero_receivers_distinguished_from_failure(event_bus: EventBus) -> None:
    """0 (delivered to nobody) and None (publish failed) are distinct return
    values — the contract callers that check receiver counts rely on. A real
    publish with no subscriber is 0, not None."""
    with redis.Redis.from_url(settings.data_plane.redis_url) as _r:  # pyright: ignore[reportUnknownMemberType]
        pass  # sanity: redis reachable
    assert event_bus.publish_best_effort_sync("{}", channel="meiva:nobody") == 0


async def test_warning_cadence_is_shared_by_bus_publishers_and_isolated_across_buses(
    loguru_records: list[dict[str, Any]],
) -> None:
    """Each bus shares sync/async warning cadence, while a new owner reports afresh."""
    failure = ResponseError("NOPERM")
    first = _bus_with(
        open_async=async_redis_factory(lambda: _BoomAsyncClient(failure)),
        open_sync=sync_redis_factory(lambda: _BoomSyncClient(failure)),
    )
    second = _bus_with(
        open_async=async_redis_factory(lambda: _BoomAsyncClient(failure)),
        open_sync=sync_redis_factory(lambda: _BoomSyncClient(failure)),
    )
    channel = "meiva:owner-cadence"

    assert first.publish_best_effort_sync("{}", channel=channel) is None
    assert await first.publish_best_effort("{}", channel=channel) is None
    assert await second.publish_best_effort("{}", channel=channel) is None
    assert first.publish_best_effort_sync("{}", channel=f"{channel}:other") is None

    assert [r["level"].name for r in loguru_records] == [
        "WARNING",
        "DEBUG",
        "WARNING",
        "WARNING",
    ]
    assert [r["exception"] is not None for r in loguru_records] == [False] * 4
    assert sum("rate-limited" in r["message"] for r in loguru_records) == 1

    cadence: dict[tuple[str, str], float] = {}
    assert should_warn_publish_failure(
        cadence, channel=channel, error_type="ResponseError", now=100.0
    )
    assert not should_warn_publish_failure(
        cadence, channel=channel, error_type="ResponseError", now=159.0
    )
    assert should_warn_publish_failure(
        cadence, channel=channel, error_type="ResponseError", now=160.0
    )


@pytest.mark.parametrize("failure", [AttributeError("bug"), DataError("invalid input")])
async def test_unknown_publish_and_client_open_errors_propagate(failure: Exception) -> None:
    async_bus = _bus_with(open_async=async_redis_factory(lambda: _BoomAsyncClient(failure)))
    with pytest.raises(type(failure)) as raised:
        await async_bus.publish_best_effort("{}")
    assert raised.value is failure

    def open_sync() -> Any:
        raise failure

    sync_bus = _bus_with(open_sync=sync_redis_factory(open_sync))
    with pytest.raises(type(failure)) as raised:
        sync_bus.publish_best_effort_sync("{}")
    assert raised.value is failure
