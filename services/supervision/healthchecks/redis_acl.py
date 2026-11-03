"""Bounded Redis runtime-credential protocol probe; no recovery authority."""

from __future__ import annotations

from typing import Protocol

import redis

TIMEOUT_S = 3.0


class RedisConnector(Protocol):
    def from_url(
        self, url: str, *, socket_connect_timeout: float, socket_timeout: float
    ) -> RedisPingConnection: ...


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
    _ping_cluster(redis.Redis, cluster_url)


# meiva:note
# PING the cluster Redis through `connector`. The redis client's stubs leave `from_url` partly
# untyped; a parameter typed with the connector protocol takes the class without naming the `Any`.
def _ping_cluster(connector: RedisConnector, cluster_url: str) -> None:
    with connector.from_url(
        cluster_url,
        socket_connect_timeout=TIMEOUT_S,
        socket_timeout=TIMEOUT_S,
    ) as r:
        r.ping()
