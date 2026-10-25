from __future__ import annotations

import pytest
from redis.exceptions import AuthenticationError

from services.supervision.healthchecks import redis_acl


class _FakeRedisConnection:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.pings = 0

    def __enter__(self) -> _FakeRedisConnection:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object, /) -> None:
        return None

    def ping(self) -> bool:
        self.pings += 1
        if self.error is not None:
            raise self.error
        return True


def test_ping_uses_runtime_url_with_bounded_socket_waits() -> None:
    conn = _FakeRedisConnection()
    redis_acl.ping("redis://runtime:credential@127.0.0.1:6380/0", connection=conn)
    assert conn.pings == 1
    assert redis_acl.TIMEOUT_S == 3.0


def test_rejected_credential_is_reported_without_repair() -> None:
    conn = _FakeRedisConnection(error=AuthenticationError("denied"))
    with pytest.raises(AuthenticationError):
        redis_acl.ping("redis://127.0.0.1:6380/0", connection=conn)
    assert not hasattr(redis_acl, "check")
    assert not hasattr(redis_acl, "main")
