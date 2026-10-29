"""Fleet graph PG failures through a router with explicit app-owned resources."""

import random
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from base.clock.tests.fakes import system_clock
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from gateway.http.middleware.error_envelope import request_trace_middleware
from gateway.http.middleware.error_handlers import unhandled_exception_handler
from gateway.lgtm.telemetry_staleness import TelemetryStaleness
from gateway.routers.fleet import fleet_graph as fg
from gateway.tests.auth.gateway_config import gateway_config_of
from gateway.tests.routers.fleet_graph.staleness_support import (
    bus_with_sync_redis,
    write_fresh_heartbeat,
)


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


@pytest.fixture
def app(database: Database) -> Iterator[FastAPI]:
    """Own the real PG pool without importing or starting unrelated gateway services."""
    redis = MagicMock()
    redis.__enter__.return_value = redis
    # A long-lived entry from an earlier shape of the cache must never be served.
    leftover = fg.FleetGraphResponse(nodes=[], edges=[]).model_dump_json()

    def get(key: str) -> str | None:
        return leftover if "last_good" in key else None

    redis.get.side_effect = get
    app = FastAPI()
    app.include_router(fg.router)
    app.middleware("http")(request_trace_middleware(_rng()))
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.state.bus = bus_with_sync_redis(lambda: redis)
    app.state.gateway_config = gateway_config_of(snapshot_process_config())
    app.state.clock = system_clock()
    app.state.telemetry_staleness = TelemetryStaleness(check_interval_s=0)
    with database.pool(max_size=2) as pool:
        write_fresh_heartbeat(pool)
        app.state.db_pool = pool
        yield app


@pytest.mark.parametrize(
    "failure",
    [
        pg_errors.QueryCanceled("canceling statement due to statement timeout"),
        pg_errors.OperationalError("connection refused"),
    ],
)
def test_pg_failure_is_a_typed_500_not_a_fallback_graph(app: FastAPI, failure: Exception) -> None:
    """Neither a statement timeout nor another PG failure is answered with a graph."""
    pool = MagicMock()
    pool.connection.side_effect = failure
    app.state.db_pool = pool

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/fleet/graph")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "nodes" not in response.json()
