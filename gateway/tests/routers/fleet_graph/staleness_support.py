from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import ConnectionPool

from gateway.lgtm.telemetry_staleness import HEARTBEAT_EVENT, TelemetryStaleness
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client


# meiva:note
# The gateway's own latency heartbeat, 30 seconds old: the guard reads it as healthy.
def write_fresh_heartbeat(pool: ConnectionPool[Any]) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
            "event_name, level, source, attributes) VALUES (%s, %s, 'm', 'c', 'p', 'telemetry', "
            "%s, 'info', 'test', %s::jsonb)",
            (
                uuid.uuid4().int % (1 << 62),
                datetime.now(UTC) - timedelta(seconds=30),
                HEARTBEAT_EVENT,
                json.dumps({}),
            ),
        )


# meiva:note
# A gateway client whose staleness guard checks on every read; the heartbeat row is real.
@contextmanager
def fleet_graph_client(*, heartbeat: bool = True, **kwargs: Any) -> Generator[GatewayTestClient]:
    with gateway_test_client(**kwargs) as client:
        client.app.state.telemetry_staleness = TelemetryStaleness(check_interval_s=0)
        if heartbeat:
            write_fresh_heartbeat(client.app.state.db_pool)
        yield client
