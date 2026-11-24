from __future__ import annotations

from pathlib import Path

import pytest

from base.telemetry import observability
from base.telemetry.tests.otlp.identity_inputs import machine_identity


@pytest.mark.parametrize(
    ("marker", "endpoint_override", "expected"),
    [
        (False, False, False),
        (True, False, True),
        (False, True, True),
        (True, True, True),
    ],
)
def test_collector_allowed_for_home_marker_or_explicit_endpoint(
    tmp_path: Path,
    marker: bool,
    endpoint_override: bool,
    expected: bool,
) -> None:
    home = tmp_path / "gateway"
    home.mkdir()
    if marker:
        (home / "lgtm-host").touch()
    assert (
        observability.collector_allowed_for_home(
            home, machine=machine_identity(), unit_home=home, endpoint_explicit=endpoint_override
        )
        is expected
    )


def test_collector_allowed_for_home_none_home_is_never_gated() -> None:
    assert (
        observability.collector_allowed_for_home(
            None, machine=machine_identity(), unit_home=Path("unused"), endpoint_explicit=False
        )
        is True
    )


def test_home_is_observability_station_marker_or_capability(tmp_path: Path) -> None:
    home = tmp_path / "station"
    home.mkdir()
    assert observability.home_is_observability_station(
        home, machine=machine_identity(roles=frozenset({"observability-station"})), unit_home=home
    )
    assert not observability.home_is_observability_station(
        home, machine=machine_identity(roles=frozenset({"gateway"})), unit_home=home
    )
    assert not observability.home_is_observability_station(
        home, machine=machine_identity(roles=None), unit_home=home
    )
    (home / "lgtm-host").touch()
    assert observability.home_is_observability_station(
        home, machine=machine_identity(roles=None), unit_home=home
    )


def test_station_capability_never_leaks_to_foreign_home(tmp_path: Path) -> None:
    own, foreign = tmp_path / "own", tmp_path / "foreign"
    own.mkdir()
    foreign.mkdir()
    machine = machine_identity(roles=frozenset({"observability-station"}))
    assert observability.home_is_observability_station(own, machine=machine, unit_home=own)
    assert not observability.home_is_observability_station(foreign, machine=machine, unit_home=own)
    (foreign / "lgtm-host").touch()
    assert observability.home_is_observability_station(foreign, machine=machine, unit_home=own)


def test_collector_allowed_for_home_accepts_station_capability(tmp_path: Path) -> None:
    machine = machine_identity(roles=frozenset({"gateway", "observability-station"}))
    assert observability.collector_allowed_for_home(
        tmp_path, machine=machine, unit_home=tmp_path, endpoint_explicit=False
    )
    assert not observability.collector_allowed_for_home(
        tmp_path, machine=machine_identity(roles=None), unit_home=tmp_path, endpoint_explicit=False
    )


def test_cluster_identity_is_derived_from_the_telemetry_token(tmp_path: Path) -> None:
    from base.cluster import cluster_id
    from base.host.db_authority.api import ensure_telemetry_token

    home = tmp_path / ".meiva"
    home.mkdir()
    assert observability.cluster_identity(home) is None
    assert observability.cluster_identity_attributes(home) == {}

    ensure_telemetry_token(home.resolve(), "fixture-token-one")

    assert observability.cluster_identity(home) == cluster_id("fixture-token-one")
    assert observability.cluster_identity_attributes(home) == {
        "meiva.cluster": cluster_id("fixture-token-one")
    }


def test_same_named_homes_share_a_label_but_not_a_routing_identity(tmp_path: Path) -> None:
    from base.host.db_authority.api import ensure_telemetry_token

    homes = [tmp_path / name / ".meiva" for name in ("a", "b")]
    for home, token in zip(homes, ("fixture-token-a", "fixture-token-b"), strict=True):
        home.mkdir(parents=True)
        ensure_telemetry_token(home.resolve(), token)

    assert observability.cluster_label(homes[0]) == observability.cluster_label(homes[1])
    assert observability.cluster_identity(homes[0]) != observability.cluster_identity(homes[1])


def test_metrics_and_trace_resources_carry_the_routing_identity(tmp_path: Path) -> None:
    from base.host.db_authority.api import ensure_telemetry_token
    from base.telemetry.otlp.telemetry_otlp_metrics import metrics_resource_attributes

    home = tmp_path / ".meiva"
    home.mkdir()
    ensure_telemetry_token(home.resolve(), "fixture-token-a")

    attributes = metrics_resource_attributes(home=home, instance_id="i")

    assert attributes["cluster"] == ".meiva"
    assert attributes["meiva.cluster"] == observability.cluster_identity(home)


def test_an_unreadable_capability_degrades_to_no_routing_identity(tmp_path: Path) -> None:
    from base.host.db_authority.unit import unit_capability_path

    home = tmp_path / ".meiva"
    capability = unit_capability_path(home.resolve())
    capability.parent.mkdir(parents=True)
    capability.write_text("not a capability")

    assert observability.cluster_identity(home) is None
    assert observability.cluster_identity_attributes(home) == {}


def test_the_logs_provider_resource_carries_the_routing_identity(tmp_path: Path) -> None:
    from base.host.db_authority.api import ensure_telemetry_token
    from base.telemetry.otlp import telemetry_otlp_sdk

    home = tmp_path / ".meiva"
    home.mkdir()
    ensure_telemetry_token(home.resolve(), "fixture-token-a")
    logs, metrics = telemetry_otlp_sdk.build_providers(
        "http://127.0.0.1:9", mirror_dir=tmp_path, home=home, instance_id="i"
    )
    try:
        assert logs.resource.attributes["meiva.cluster"] == observability.cluster_identity(home)
        assert metrics._sdk_config.resource.attributes["meiva.cluster"] == (
            observability.cluster_identity(home)
        )
    finally:
        logs.shutdown()
        metrics.shutdown()


def test_the_process_tracer_resource_carries_the_routing_identity(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from base.host.db_authority.api import ensure_telemetry_token
    from base.telemetry.tests.otlp.identity_inputs import machine_identity
    from base.telemetry.tracing import build_process_tracer

    home = tmp_path / ".meiva"
    home.mkdir()
    ensure_telemetry_token(home.resolve(), "fixture-token-a")
    settings = SimpleNamespace(
        trace_enabled=True,
        trace_strip_content=True,
        telemetry_otlp_endpoint="http://127.0.0.1:9",
    )

    provider = build_process_tracer(
        service="test",
        machine=machine_identity(),
        home=home,
        settings=settings,
        environ={"MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://127.0.0.1:9"},
    )

    assert provider is not None
    assert provider.resource.attributes["meiva.cluster"] == observability.cluster_identity(home)
    assert provider.resource.attributes["cluster"] == ".meiva"
