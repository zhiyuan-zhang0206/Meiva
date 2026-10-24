# meiva:note
# Who may reach a collector's receivers and what each topology relays: the bearer-authenticated
# remote ingress of a gateway or station, the pure runner's relay to its gateway, a gateway that
# consumes a remote observatory, and every incomplete identity that must fail closed. Each case
# renders the shipped template through the shell with values passed in.
# meiva:link docs/conventions/data/reachability-and-credentials.md

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.cluster.machines import unit_dial_url
from base.host.db_authority.unit import telemetry_bearer
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    GATEWAY,
    GATEWAY_INGRESS,
    HYBRID,
    RUNNER,
    STATION,
    CollectorUnit,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import data_plane, observability
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

SECRET = data_plane().cluster_secret
RELAY_EXPORTERS = ("otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus")


def _bearer(unit: CollectorUnit) -> str:
    bearer = telemetry_bearer(unit.home.resolve(), SECRET)
    assert bearer is not None
    return bearer


def _http(config: dict[str, Any]) -> dict[str, Any]:
    return config["receivers"]["otlp"]["protocols"]["http"]


def test_a_pure_runner_relays_every_signal_to_the_gateway_ingress_with_the_derived_bearer(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(
        RUNNER, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS)
    )

    assert set(config["receivers"]) >= {"otlp", "host_metrics", "prometheus/otelcol"}
    assert "otlp/remote" not in config["receivers"]
    assert set(config["exporters"]) == {*RELAY_EXPORTERS, "file/traces"}
    bearer = _bearer(collector_unit)
    assert bearer != SECRET
    for exporter_id in RELAY_EXPORTERS:
        exporter = config["exporters"][exporter_id]
        assert exporter["endpoint"] == GATEWAY_INGRESS
        assert exporter["headers"] == {"Authorization": f"Bearer {bearer}"}
    rendered = str(config)
    for loopback_backend in ("127.0.0.1:14318", "127.0.0.1:3100", "127.0.0.1:9090"):
        assert loopback_backend not in rendered


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(GATEWAY, id="gateway"),
        pytest.param(STATION, id="station"),
        pytest.param(HYBRID, id="hybrid"),
    ],
)
def test_a_gateway_or_station_accepts_remote_relays_only_on_its_reachable_address_with_bearer_auth(
    collector_unit: CollectorUnit, roles: frozenset[str]
) -> None:
    config = collector_unit.render(roles)

    assert _http(config) == {"endpoint": "127.0.0.1:4318"}
    assert config["receivers"]["otlp/remote"]["protocols"]["http"] == {
        "endpoint": "10.0.0.10:4318",
        "auth": {"authenticator": "bearertokenauth/cluster"},
    }
    assert config["extensions"]["bearertokenauth/cluster"] == {"token": _bearer(collector_unit)}
    assert "bearertokenauth/cluster" in config["service"]["extensions"]
    pipelines = config["service"]["pipelines"]
    assert pipelines["traces/remote"]["exporters"] == ["otlphttp/tempo"]
    assert pipelines["traces"]["exporters"] == ["otlphttp/tempo", "file/traces"]
    assert "otlp/remote" in pipelines["logs"]["receivers"]
    assert "otlp/remote" in pipelines["metrics"]["receivers"]
    for exporter_id, endpoint in (
        ("otlphttp/tempo", "http://127.0.0.1:14318"),
        ("otlphttp/loki", "http://127.0.0.1:3100/otlp"),
        ("otlphttp/prometheus", "http://127.0.0.1:9090/api/v1/otlp"),
    ):
        assert config["exporters"][exporter_id]["endpoint"] == endpoint


def test_the_stations_remote_receiver_binds_the_port_its_unit_url_advertises(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(STATION, observability=observability(telemetry_otlp_port=4321))

    assert config["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == "10.0.0.10:4321"
    assert (
        unit_dial_url(
            STATION,
            host="10.0.0.10",
            ops_port=9000,
            gateway_port=8000,
            telemetry_otlp_port=4321,
            gateway=None,
        )
        == "http://10.0.0.10:4321"
    )


def test_the_local_listener_follows_this_units_port_while_relay_targets_follow_bootstrap(
    collector_unit: CollectorUnit,
) -> None:
    local = observability(telemetry_otlp_port=4319, gateway_otlp_endpoint=GATEWAY_INGRESS)

    gateway = collector_unit.render(GATEWAY, observability=local)
    runner = collector_unit.render(RUNNER, observability=local)

    assert _http(gateway) == {"endpoint": "127.0.0.1:4319"}
    assert gateway["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"] == "10.0.0.10:4319"
    for exporter_id in RELAY_EXPORTERS:
        assert runner["exporters"][exporter_id]["endpoint"] == GATEWAY_INGRESS


def test_an_ipv6_address_is_bracketed_in_the_receiver_bind_and_in_the_relay_endpoint(
    collector_unit: CollectorUnit,
) -> None:
    gateway = collector_unit.render(GATEWAY, host="fd7a:115c:a1e0::10")
    runner = collector_unit.render(
        RUNNER,
        host="fd7a:115c:a1e0::20",
        observability=observability(gateway_otlp_endpoint="http://[fd7a:115c:a1e0::10]:4318"),
    )

    remote = gateway["receivers"]["otlp/remote"]["protocols"]["http"]["endpoint"]
    assert remote == "[fd7a:115c:a1e0::10]:4318"
    for exporter_id in RELAY_EXPORTERS:
        assert runner["exporters"][exporter_id]["endpoint"] == "http://[fd7a:115c:a1e0::10]:4318"


@pytest.mark.parametrize(
    ("roles", "host", "secret"),
    [
        pytest.param(HYBRID, "localhost", "", id="single-box"),
        pytest.param(HYBRID, "localhost", SECRET, id="single-box-with-a-secret"),
        pytest.param(GATEWAY, "10.0.0.10", "", id="gateway-without-a-secret"),
        pytest.param(STATION, "10.0.0.10", "", id="station-without-a-secret"),
        pytest.param(GATEWAY, "localhost", SECRET, id="gateway-on-loopback"),
        pytest.param(STATION, "localhost", SECRET, id="station-on-loopback"),
    ],
)
def test_nothing_remote_can_dial_a_unit_without_a_secret_or_a_routable_host(
    collector_unit: CollectorUnit, roles: frozenset[str], host: str, secret: str
) -> None:
    config = collector_unit.render(roles, host=host, data_plane=data_plane(cluster_secret=secret))

    assert "otlp/remote" not in config["receivers"]
    assert "bearertokenauth/cluster" not in config["extensions"]
    assert _http(config)["endpoint"] == "127.0.0.1:4318"
    assert all(
        exporter["endpoint"].startswith("http://127.0.0.1:")
        for name, exporter in config["exporters"].items()
        if name.startswith("otlphttp/")
    )


# meiva:note
# The wildcard host is a rejection fixture: the render refuses it and nothing binds to it.
@pytest.mark.parametrize(
    ("roles", "host", "secret", "gateway_ingress", "message"),
    [
        pytest.param(RUNNER, "10.0.0.20", SECRET, "", "gateway bootstrap", id="no-ingress"),
        pytest.param(
            RUNNER,
            "10.0.0.20",
            SECRET,
            "http://localhost:4318",
            "gateway bootstrap",
            id="loopback-ingress",
        ),
        pytest.param(
            RUNNER, "10.0.0.20", "", GATEWAY_INGRESS, "telemetry token", id="runner-no-secret"
        ),
        pytest.param(
            GATEWAY,
            "0.0.0.0",  # noqa: S104
            SECRET,
            "",
            "reachable host",
            id="gateway-wildcard-host",
        ),
        pytest.param(STATION, "::", SECRET, "", "reachable host", id="station-wildcard-host"),
    ],
)
def test_a_split_topology_fails_closed_when_its_ingress_identity_is_incomplete(
    collector_unit: CollectorUnit,
    roles: frozenset[str],
    host: str,
    secret: str,
    gateway_ingress: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        collector_unit.render(
            roles,
            host=host,
            observability=observability(gateway_otlp_endpoint=gateway_ingress),
            data_plane=data_plane(cluster_secret=secret),
        )


@pytest.mark.parametrize(
    "roles",
    [pytest.param(None, id="unconfigured"), pytest.param(HYBRID, id="hybrid")],
)
def test_a_unit_that_consumes_a_remote_observatory_relays_every_signal_to_its_single_ingress(
    db_url: str,
    collector_unit: CollectorUnit,
    operator_database: Callable[[], Any],
    roles: frozenset[str] | None,
) -> None:
    config = collector_unit.render(
        roles,
        observability=observability(observability_url="http://10.0.0.46"),
        database_factory=operator_database,
    )

    bearer = _bearer(collector_unit)
    for exporter_id in RELAY_EXPORTERS:
        exporter = config["exporters"][exporter_id]
        assert exporter["endpoint"] == "http://10.0.0.46:4318"
        assert exporter["headers"] == {"Authorization": f"Bearer {bearer}"}
    rendered = str(config)
    for loopback_backend in ("127.0.0.1:3100", "127.0.0.1:9090", "127.0.0.1:14318"):
        assert loopback_backend not in rendered
    assert "file/traces" in config["exporters"]
    assert _http(config) == {"endpoint": "127.0.0.1:4318"}


def test_a_remote_observatory_without_a_cluster_secret_cannot_authenticate_its_relay(
    db_url: str, collector_unit: CollectorUnit, operator_database: Callable[[], Any]
) -> None:
    with pytest.raises(RuntimeError, match="telemetry token"):
        collector_unit.render(
            HYBRID,
            observability=observability(observability_url="http://10.0.0.46"),
            data_plane=data_plane(cluster_secret=""),
            database_factory=operator_database,
        )
