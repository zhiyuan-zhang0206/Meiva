from __future__ import annotations

from collections.abc import Callable
from typing import cast

from base.events.live.redis_client import AuthRetryAsyncRedis, AuthRetrySyncRedis


# meiva:note
# `EventBus` takes the client openers it dials with, so a test hands in the client it needs
# instead of rewriting the class or the module-level opener. The cast states the contract a fake
# narrows deliberately: it answers the commands the code under test calls, not the whole redis-py
# client. The opener ignores the URL, the decode flag and the retry policy the bus passes,
# because the client is already built.
def async_redis_factory(make_client: Callable[[], object]) -> Callable[..., AuthRetryAsyncRedis]:
    def open_async_redis(*_args: object, **_kwargs: object) -> AuthRetryAsyncRedis:
        return cast(AuthRetryAsyncRedis, make_client())

    return open_async_redis


# meiva:note
# The sync counterpart of `async_redis_factory`: one client per call, as `sync_redis()` opens.
def sync_redis_factory(make_client: Callable[[], object]) -> Callable[..., AuthRetrySyncRedis]:
    def open_sync_redis(*_args: object, **_kwargs: object) -> AuthRetrySyncRedis:
        return cast(AuthRetrySyncRedis, make_client())

    return open_sync_redis
