import asyncio

import pytest

from base.clock import SYSTEM_TIMERS
from base.events.live import redis_listener


class _PubSub:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.open_task: asyncio.Task[object] | None = None
        # meiva:note
        # Holds whatever task the caller parked here and only ever cancels it. `wait_one`
        # answers bool while redis's own `PubSub.parent` is typed `Task[None]`, so the slot
        # takes the task's value type as `object` instead of demanding None.
        self.parent: asyncio.Task[object] | None = None
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.open_task = asyncio.current_task()
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.parent is not None:
            asyncio.get_running_loop().call_soon(self.parent.cancel)

    async def aclose(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self, pubsub: _PubSub) -> None:
        self.subscription = pubsub
        self.close_entered = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False

    def pubsub(self, **kwargs: object) -> _PubSub:
        return self.subscription

    async def aclose(self) -> None:
        self.close_entered.set()
        await self.close_release.wait()
        self.closed = True


@pytest.mark.parametrize("eager", [False, True])
async def test_cancelling_open_wait_cancels_child_without_waiting_for_rollback(
    eager: bool,
) -> None:
    pubsub = _PubSub()
    redis = _Redis(pubsub)

    def _factory(_url: str, **_kw: object) -> _Redis:
        return redis

    listener = redis_listener.RedisInboundListener(
        "redis://unused",
        7001,
        redis_factory=_factory,
        events_channel="meiva:test:events",
        timers=SYSTEM_TIMERS,
    )
    parent = asyncio.create_task(listener.ensure_listening() if eager else listener.wait_one(30))
    try:
        await pubsub.entered.wait()
        parent.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 1)
        await asyncio.wait_for(pubsub.cancelled.wait(), 1)
        await asyncio.wait_for(redis.close_entered.wait(), 1)
        assert parent.done()
        assert not redis.closed
        assert listener._pubsub is None
        assert listener._redis is None
    finally:
        redis.close_release.set()
        assert pubsub.open_task is not None
        if not pubsub.cancelled.is_set():
            pubsub.open_task.cancel()
        await asyncio.gather(pubsub.open_task, return_exceptions=True)
        await listener.close()
    assert redis.closed


async def test_cancelling_parent_after_open_completes_preserves_listener_handles() -> None:
    pubsub = _PubSub()
    redis = _Redis(pubsub)
    redis.close_release.set()

    def _factory(_url: str, **_kw: object) -> _Redis:
        return redis

    listener = redis_listener.RedisInboundListener(
        "redis://unused",
        7002,
        redis_factory=_factory,
        events_channel="meiva:test:events",
        timers=SYSTEM_TIMERS,
    )
    parent = asyncio.create_task(listener.wait_one(30))
    pubsub.parent = parent
    try:
        await pubsub.entered.wait()
        pubsub.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(parent, 1)
        assert pubsub.open_task is not None
        assert pubsub.open_task.done()
        assert not pubsub.open_task.cancelled()
        assert listener._pubsub is pubsub
        assert listener._redis is redis
        assert not pubsub.closed
        assert not redis.closed
    finally:
        await listener.close()
    assert pubsub.closed
    assert redis.closed
    assert listener._pubsub is None
    assert listener._redis is None
