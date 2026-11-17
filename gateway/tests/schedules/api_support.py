# meiva:note
# Gateway clients for schedule API tests.

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from typing import Any

from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)

__all__ = ["schedule_client"]


# meiva:note
# A gateway client whose requests wait only `wait_s` for a manager to consume them.
@contextmanager
def schedule_client(
    *, wait_s: float = 0.1, fields: Mapping[str, Any] | None = None, **kwargs: Any
) -> Generator[GatewayTestClient]:
    config = gateway_test_config(
        fields, schedule_consume_wait_seconds=wait_s, schedule_consume_poll_seconds=0.01
    )
    with gateway_test_client(config=config, **kwargs) as client:
        yield client
