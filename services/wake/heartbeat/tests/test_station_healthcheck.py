"""`services.wake.heartbeat.station_probe` unit tests — the remote-station probe.

WP4 (task #1946): the gateway probes a REMOTE observatory station through
the reachability contract (the address the station advertises in
machine_units) with the cluster bearer, and FAILS OPEN — a failed probe
never blocks or restarts local business; a failed probe only logs. With no
MEIVA_OBSERVABILITY_URL configured the check is a no-op.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import base.db
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.db_authority.api import (
    publish_telemetry_token,
    read_telemetry_token,
    telemetry_token,
    telemetry_token_path,
)
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.station_endpoint import StationEndpointConfig, StationTarget
from services.wake.heartbeat import station_probe as hc


@pytest.fixture
def _log_boundary(monkeypatch: pytest.MonkeyPatch, unit_home: Path) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", Mock())
    monkeypatch.setattr(hc, "build_pipeline", Mock(return_value=object()))


@pytest.fixture(autouse=True)
def _clean_state(*, db_url: str, database_gate: ProcessDbGate) -> None:
    """Reset the in-process probe state between tests (the module dict is
    process-global, same as the watchdog would hold it)."""
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM machine_units WHERE machine_name LIKE 'station-test%'")
        conn.commit()


@pytest.fixture
def probe_token(unit_home: Path) -> str:
    home = unit_home
    publish_telemetry_token(home, "cluster-token")
    token = read_telemetry_token(home)
    assert token is not None
    return token


def _insert_station_unit(
    name: str = "station-test-a", url: str = "http://10.0.0.9:4318", *, database_gate: ProcessDbGate
) -> None:
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_units "
            "(machine_name, home, serve_gateway, serve_agent_runner, "
            "serve_observability_station, url, up_since_at, stopped_at) "
            "VALUES (%s, %s, false, false, true, %s, now(), NULL)",
            (name, "~/.meiva_station", url),
        )
        conn.commit()


@pytest.mark.usefixtures("_log_boundary")
def test_main_is_noop_without_remote_observatory(monkeypatch: pytest.MonkeyPatch) -> None:
    process_config = snapshot_process_config()
    process_config.set_field("observability_url", "")
    create = Mock(side_effect=AssertionError("a no-op probe must not open its work database"))
    monkeypatch.setattr(hc.Database, "from_settings", create)
    opener = FakeUrlOpener()
    hc.main(process_config=process_config, open_url=opener)
    assert opener.calls == []
    create.assert_not_called()


def test_resolve_target_uses_advertised_machine_units_url(
    database: Database, *, database_gate: ProcessDbGate
) -> None:
    """The probe target is the station's ADVERTISED machine_units url — the
    reachability contract — not the configured base."""
    _insert_station_unit(database_gate=database_gate)
    target = hc.resolve_target(
        database=lambda: database,
        config=StationEndpointConfig("http://10.0.0.9", 4325),
    )
    assert target is not None
    assert target.url == "http://10.0.0.9:4318"
    assert target.advertised is True
    assert target.name == "station-test-a"


def test_resolve_target_falls_back_to_configured_base_without_registration(
    database: Database,
) -> None:
    """MEIVA_OBSERVABILITY_URL set but no station unit registered -> warn loudly
    and probe the configured base + OTLP port (fail-open: still give signal)."""
    target = hc.resolve_target(
        database=lambda: database,
        config=StationEndpointConfig("http://10.0.0.46", 4325),
    )
    assert target is not None
    assert target.url == "http://10.0.0.46:4325"
    assert target.advertised is False


def test_resolve_target_skips_hybrid_gateway_station_units(
    database: Database, *, database_gate: ProcessDbGate
) -> None:
    """A hybrid gateway+station unit advertises its GATEWAY url (unit_dial_url
    lets the gateway capability win), not the OTLP ingress — probing it would
    hit the gateway API and alert forever (QA #1156 NIT-2). Only units whose
    capability set identifies an ingress advertisement qualify."""
    _insert_station_unit(url="http://10.0.0.46:4318", database_gate=database_gate)
    with base.db.connect(gate=database_gate) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE machine_units SET serve_gateway = true WHERE machine_name = 'station-test-a'"
        )
        conn.commit()
    target = hc.resolve_target(
        database=lambda: database,
        config=StationEndpointConfig("http://10.0.0.46", 4318),
    )
    assert target is not None
    assert target.url == "http://10.0.0.46:4318"
    assert target.advertised is False


@pytest.mark.parametrize("observability_url", ["", "http://station.invalid:3100"])
def test_resolve_target_none_without_valid_observability_url(observability_url: str) -> None:
    create = Mock(side_effect=AssertionError("an unconfigured probe must not open its database"))
    assert (
        hc.resolve_target(database=create, config=StationEndpointConfig(observability_url, 4318))
        is None
    )
    create.assert_not_called()


@pytest.mark.parametrize("status", [200, 202, 204])
def test_station_answers_bearer_otlp_roundtrip(probe_token: str, status: int) -> None:
    """A 2xx on POST <url>/v1/traces with the telemetry bearer = alive."""
    opener = FakeUrlOpener()
    opener.on("http://10.0.0.9:4318/v1/traces", Answer(status=status))
    assert hc.station_answers("http://10.0.0.9:4318/", token=probe_token, open_url=opener) is True
    assert len(opener.calls) == 1
    request = opener.calls[0]
    assert request.url == "http://10.0.0.9:4318/v1/traces"
    assert request.method == "POST"
    assert request.data == b'{"resourceSpans":[]}'
    assert request.timeout == 5.0
    assert request.headers["Content-type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {telemetry_token('cluster-token')}"


@pytest.mark.parametrize("status", [401, 415, 503])
def test_station_answers_http_error_is_not_alive(probe_token: str, status: int) -> None:
    opener = FakeUrlOpener()
    opener.on("http://10.0.0.9:4318", Answer(status=status))
    assert hc.station_answers("http://10.0.0.9:4318", token=probe_token, open_url=opener) is False
    assert len(opener.calls) == 1


@pytest.mark.parametrize("error", [OSError("connection refused"), TimeoutError("probe timed out")])
def test_station_answers_connection_failure_is_not_alive(
    probe_token: str, error: OSError, loguru_records: list[dict[str, Any]]
) -> None:
    opener = FakeUrlOpener()
    opener.on("http://10.0.0.9:4318", error)
    assert hc.station_answers("http://10.0.0.9:4318", token=probe_token, open_url=opener) is False
    assert len(opener.calls) == 1
    record = next(r for r in loguru_records if "unreachable" in r["message"])
    assert record["exception"] is not None
    assert record["exception"].value is error


def test_station_answers_without_a_published_token_warns_and_skips(
    loguru_records: list[dict[str, Any]],
) -> None:
    opener = FakeUrlOpener()
    assert hc.station_answers("http://10.0.0.9:4318", token=None, open_url=opener) is None
    assert opener.calls == []
    assert any("no published telemetry token" in record["message"] for record in loguru_records)


@pytest.mark.usefixtures("_log_boundary")
def test_main_probes_advertised_station_and_does_not_raise(
    probe_token: str,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    """End-to-end: with a registered station, main() runs clean on a healthy
    probe and fails open on a failing one."""
    process_config = snapshot_process_config()
    process_config.set_field("observability_url", "http://10.0.0.9")
    _insert_station_unit(database_gate=database_gate)
    opener = FakeUrlOpener()
    opener.on("http://10.0.0.9:4318", Answer(), Answer(status=503))
    hc.main(process_config=process_config, open_url=opener)
    hc.main(process_config=process_config, open_url=opener)  # must not raise
    assert opener.urls == ["http://10.0.0.9:4318/v1/traces"] * 2
    assert all(call.headers["Authorization"] == f"Bearer {probe_token}" for call in opener.calls)

    failed_observations = sum(
        "did not answer a bearer OTLP probe" in r["message"] for r in loguru_records
    )
    assert failed_observations == 1
    telemetry_token_path(meiva_home()).unlink()
    hc.main(process_config=process_config, open_url=opener)
    assert len(opener.calls) == 2
    assert any("no published telemetry token" in record["message"] for record in loguru_records)
    assert (
        sum("did not answer a bearer OTLP probe" in r["message"] for r in loguru_records)
        == failed_observations
    )


def test_main_shares_one_captured_gate_with_the_probe_and_log_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe_token: str
) -> None:
    process_config = snapshot_process_config()
    process_config.set_field("observability_url", "http://station.invalid")
    process_config.set_field("observability_otlp_port", 44318)
    image = LoadedCommit(tmp_path, "captured-before-checkout-moved")
    capture = Mock(return_value=image)
    count = Mock(return_value=7)
    create = Mock(return_value=Mock(spec=Database))
    target = StationTarget(url="http://station.invalid:4318", advertised=True)
    received: list[object] = []

    def resolve(
        *, database: Callable[[], Database], config: StationEndpointConfig
    ) -> StationTarget:
        assert config == StationEndpointConfig("http://station.invalid", 44318)
        received.append(database)
        database()
        return target

    logging = Mock()
    pipeline = Mock(return_value=object())
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(hc.Database, "from_settings", create)
    monkeypatch.setattr(hc, "resolve_target", resolve)
    monkeypatch.setattr(hc, "build_pipeline", pipeline)
    monkeypatch.setattr(hc, "init_gateway_process", logging)
    opener = FakeUrlOpener()
    opener.on(target.url, Answer())
    hc.main(process_config=process_config, open_url=opener)
    assert len(opener.calls) == 1
    assert opener.calls[0].headers["Authorization"] == f"Bearer {probe_token}"

    capture.assert_called_once_with()
    count.assert_not_called()
    factory = pipeline.call_args.kwargs["database"]
    assert received == [factory]
    assert logging.call_args.kwargs["image"] is image
    assert logging.call_args.kwargs["producer"]() is pipeline.return_value
    factory()
    gates = [call.kwargs["gate"] for call in create.call_args_list]
    assert len(gates) == 2 and gates[0] is gates[1]
    assert isinstance(gates[0], ProcessDbGate)
    assert gates[0].application_name() == "meiva:station:v7"
    count.assert_called_once_with(tmp_path, image.sha)
    gates[0].observe_minimum(7)
    assert not gates[0].min_read_due()


def test_station_lookup_keeps_the_original_fail_open_boundary() -> None:
    create = Mock(side_effect=RuntimeError("original database failure"))
    assert (
        hc.resolve_target(
            database=create, config=StationEndpointConfig("http://station.invalid", 4318)
        )
        is None
    )
    create.assert_called_once_with()
