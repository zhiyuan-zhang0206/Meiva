from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client


def fresh_heartbeat_age(pool: object, *, now: datetime) -> float:
    del pool, now
    return 30.0


@contextmanager
def fleet_graph_client(
    *,
    read_heartbeat_age: Callable[..., float | None] = fresh_heartbeat_age,
    **kwargs: Any,
) -> Generator[GatewayTestClient]:
    with gateway_test_client(**kwargs) as client:
        client.app.state.telemetry_staleness = TelemetryStaleness(
            check_interval_s=0, read_heartbeat_age=read_heartbeat_age
        )
        yield client
