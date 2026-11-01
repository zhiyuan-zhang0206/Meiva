"""Explicit gateway failures eligible for bounded retry and deferred delivery."""

import json
from typing import Any, Protocol, TypeGuard

# 500 is an unknown server failure. The caller must see its original response.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


class GatewayResponse(Protocol):
    """The slice of ``httpx.Response`` the retry policy reads.

    Declared structurally so this module, which loads on the ``import meiva`` path through the
    gateway transport, does not import httpx at module level.
    """

    @property
    def status_code(self) -> int: ...

    def json(self, **kwargs: object) -> object: ...


def network_errors() -> tuple[type[Exception], ...]:
    """The httpx transport failures that leave the gateway reachable on a later attempt."""
    import httpx  # deferred: `import meiva` reaches this module and must not load the HTTP stack

    return (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.RemoteProtocolError,
        httpx.ProxyError,
    )


def retryable_response(response: GatewayResponse) -> bool:
    """Known transient status, unless the wire refuses retry or proves commit.

    A proxy's non-JSON error page retains its status policy. Structured gateway
    responses may narrow it; malformed boolean controls are protocol errors.
    The response itself stays intact so callers retain the durable receipt.
    """
    if response.status_code not in TRANSIENT_HTTP_STATUSES:
        return False
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not _is_str_dict(body):
        return True
    for field in ("retryable", "committed"):
        if field in body and not isinstance(body[field], bool):
            raise ValueError(f"Gateway response {field} must be a boolean")
    return body.get("retryable") is not False and body.get("committed") is not True


def retryable_database_error(error: Exception) -> bool:
    """Database connection availability, excluding pool lifecycle and query errors."""
    from psycopg import OperationalError
    from psycopg_pool import PoolTimeout

    from base.db.protocols import RestoreAbandonedError

    return (
        isinstance(error, (PoolTimeout, RestoreAbandonedError))
        or (type(error) is OperationalError and error.sqlstate is None)
        or (
            isinstance(error, OperationalError)
            and error.sqlstate is not None
            and error.sqlstate.startswith("08")
        )
    )
