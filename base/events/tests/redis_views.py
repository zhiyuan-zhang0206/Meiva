from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self, cast

import redis
import redis.asyncio


# meiva:note
# The redis-py calls the fixture tests make, typed here because redis-py leaves their client and
# response types partly Unknown. The clients are built through `Redis.from_url` looked up at call
# time, so a test that patches it still reaches the patched function.
class PipelineCalls(Protocol):
    def ping(self) -> object: ...

    def execute(self) -> object: ...


class RedisCalls(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> object: ...

    def ping(self) -> object: ...

    def flushdb(self) -> object: ...

    def pipeline(self) -> PipelineCalls: ...


class AsyncPipelineCalls(Protocol):
    def ping(self) -> object: ...

    async def execute(self) -> object: ...


class AsyncRedisCalls(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> object: ...

    async def ping(self) -> object: ...

    def pipeline(self) -> AsyncPipelineCalls: ...


def sync_redis(url: str) -> RedisCalls:
    return cast("Callable[[str], RedisCalls]", redis.Redis.from_url)(url)


def async_redis(url: str) -> AsyncRedisCalls:
    return cast("Callable[[str], AsyncRedisCalls]", redis.asyncio.Redis.from_url)(url)
