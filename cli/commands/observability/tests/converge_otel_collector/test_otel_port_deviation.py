# meiva:note
# The observability station's advertised address drives the collector render and the station
# probe alike. A station that registers its OTLP ingress in `machine_units` is dialed on that
# port; one that has not registered is dialed on the configured station port. A pure runner never
# looks the station up: it relays to its gateway.

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base import db
from base.db.code_version_gate import ProcessDbGate
from base.telemetry import station_endpoint
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    GATEWAY,
    GATEWAY_INGRESS,
    RUNNER,
    CollectorUnit,
    never_dialed,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import observability
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.wake.heartbeat import station_probe


@pytest.mark.parametrize("station_port", [4318, 4325])
def test_remote_station_advertisement_drives_render_and_probe(
    db_url: str,
    station_port: int,
    collector_unit: CollectorUnit,
    operator_database: Callable[[], Any],
    database_gate: ProcessDbGate,
) -> None:
    values = observability(
        telemetry_otlp_port=4319,
        otel_collector_metrics_port=8889,
        observability_url="http://10.0.0.46",
    )
    with db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, serve_observability_station, url) "
            "VALUES ('station-deviation', '/station-deviation', false, false, true, %s)",
            (f"http://10.0.0.46:{station_port}",),
        )
        conn.commit()
    try:
        config = collector_unit.render(
            GATEWAY, observability=values, database_factory=operator_database
        )
        endpoint = f"http://10.0.0.46:{station_port}"
        for name in ("tempo", "loki", "prometheus"):
            assert config["exporters"][f"otlphttp/{name}"]["endpoint"] == endpoint
        assert config["receivers"]["otlp"]["protocols"]["http"]["endpoint"] == "127.0.0.1:4319"
        assert oc.lgtm_fanout_bases(values, remote=True, database_factory=operator_database) == (
            endpoint,
            endpoint,
        )
        target = station_probe.resolve_target(
            database=operator_database,
            config=station_endpoint.StationEndpointConfig(
                values.observability_url, values.observability_otlp_port
            ),
        )
        assert target is not None and target.advertised and target.url == endpoint
    finally:
        with db.connect(gate=database_gate) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM machine_units WHERE machine_name = 'station-deviation'")
            conn.commit()


def test_unregistered_station_uses_remote_port_projection(
    db_url: str, operator_database: Callable[[], Any]
) -> None:
    values = observability(
        telemetry_otlp_port=4319,
        observability_otlp_port=4325,
        observability_url="http://192.0.2.45",
    )

    ingress = oc.station_otel_ingress_endpoint(values, database_factory=operator_database)

    assert ingress == "http://192.0.2.45:4325"
    target = station_probe.resolve_target(
        database=operator_database,
        config=station_endpoint.StationEndpointConfig("http://192.0.2.45", 4325),
    )
    assert target is not None and not target.advertised
    assert target.url == ingress


def test_runner_does_not_discover_station(collector_unit: CollectorUnit) -> None:
    config = collector_unit.render(
        RUNNER,
        observability=observability(
            observability_url="http://10.0.0.46", gateway_otlp_endpoint=GATEWAY_INGRESS
        ),
        database_factory=never_dialed,
    )

    assert config["exporters"]["otlphttp/tempo"]["endpoint"] == GATEWAY_INGRESS
