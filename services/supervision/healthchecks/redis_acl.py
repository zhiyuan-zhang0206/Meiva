"""Bounded Redis runtime-credential protocol probe; no recovery authority."""

from __future__ import annotations

from typing import Any, Protocol

import redis

TIMEOUT_S = 3.0


class RedisPingConnection(Protocol):
    def __enter__(self) -> RedisPingConnection: ...

    def __exit__(self, exc_type: object, exc: object, tb: object, /) -> object: ...

    def ping(self) -> object: ...


def ping(cluster_url: str, *, connection: RedisPingConnection | None = None) -> None:
    """PING redis as the cluster identity; raises on auth failure or dead server."""
    if connection is not None:
        with connection as r:
            r.ping()
        return
    redis_cls: Any = redis.Redis
    with redis_cls.from_url(
        cluster_url,
        socket_connect_timeout=TIMEOUT_S,
        socket_timeout=TIMEOUT_S,
    ) as r:
        r.ping()
