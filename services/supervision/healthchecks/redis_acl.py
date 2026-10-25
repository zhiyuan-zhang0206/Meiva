"""Bounded Redis runtime-credential protocol probe; no recovery authority."""

from collections.abc import Callable
from typing import Any

import redis

_TIMEOUT_S = 3.0


def _default_from_url(url: str, *, socket_connect_timeout: float, socket_timeout: float) -> object:
    redis_cls: Any = redis.Redis
    return redis_cls.from_url(
        url,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
    )


def ping(
    cluster_url: str,
    *,
    from_url: Callable[..., object] = _default_from_url,
) -> None:
    """PING redis as the cluster identity; raises on auth failure or dead server."""
    conn: Any = from_url(cluster_url, socket_connect_timeout=_TIMEOUT_S, socket_timeout=_TIMEOUT_S)
    with conn as r:
        r.ping()
