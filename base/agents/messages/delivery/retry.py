# meiva:note
# The two retry policies, decided here and only here: the gateway one reads the
# response itself, and the database one classifies a failure from facts its driver
# adapter passes in. retry_io.py is the only module that names psycopg, so a caller
# that needs the policy alone never loads the driver.

import json
from typing import Protocol

from base.host.boundary.json_values import is_object_dict

# 500 is an unknown server failure. The caller must see its original response.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})


class GatewayResponse(Protocol):
    """The slice of ``httpx.Response`` the retry policy reads.

    Declared structurally so this module, which loads on the ``import meiva`` path through the
    gateway transport, does not import httpx at module level.
    """

    @property
    def status_code(self) -> int: ...

    def json(self, **kwargs: object) -> object: ...


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
    if not is_object_dict(body):
        return True
    for field in ("retryable", "committed"):
        if field in body and not isinstance(body[field], bool):
            raise ValueError(f"Gateway response {field} must be a boolean")
    return body.get("retryable") is not False and body.get("committed") is not True


# meiva:note
# What makes a database failure connection availability: the two first-party
# lifecycle failures are transient, and so is a driver OperationalError with no
# sqlstate (the connection never reached the server) or a class-08 sqlstate
# (connection exception). The no-sqlstate branch admits only the exact type, so a
# subclass that answers no sqlstate stays a hard failure.
def transient_database_failure(
    *,
    pool_timeout: bool,
    abandoned_restore: bool,
    operational_error: bool,
    operational_subclass: bool,
    sqlstate: str | None,
) -> bool:
    return (
        pool_timeout
        or abandoned_restore
        or (operational_error and sqlstate is None)
        or (operational_subclass and sqlstate is not None and sqlstate.startswith("08"))
    )
