"""`services.wake.heartbeat.station_probe` unit tests — the remote-station probe.

WP4 (task #1946): the gateway probes a REMOTE observatory station through
the reachability contract (the address the station advertises in
machine_units) with the cluster bearer, and FAILS OPEN — a failed probe
never blocks or restarts local business; a failed probe only logs. With no
MEIVA_OBSERVABILITY_URL configured the check is a no-op.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

import base.db
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.configuration import snapshot_process_config
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.db_authority.api import (
    publish_telemetry_token,
    read_telemetry_token,
    telemetry_token,
    telemetry_token_path,
)
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.paths import meiva_home
from base.telemetry.delivery.receipts import DrainStatus
from base.telemetry.pipeline_build import build_pipeline
from base.telemetry.station_endpoint import StationEndpointConfig
from services.wake.heartbeat import station_probe as hc


def _refused(db_url: str) -> str:
    parts = conninfo_to_dict(db_url)
    return f"postgresql://{parts['user']}@127.0.0.1:1/{parts['dbname']}"


def _application_name(database: Database) -> str:
    with database.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
    assert row is not None
    return str(row[0])


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


def test_main_is_noop_without_remote_observatory(unit_home: Path) -> None:
    process_config = snapshot_process_config()
    process_config.set_field("observability_url", "")

    def fail_database() -> Database:
        raise AssertionError("a no-op probe must not open its work database")

    opener = FakeUrlOpener()
    hc.run(
        process_config=process_config,
        database=fail_database,
        home=unit_home,
        open_url=opener,
    )
    assert opener.calls == []


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
    def fail_database() -> Database:
        raise AssertionError("an unconfigured probe must not open its database")

    assert (
        hc.resolve_target(
            database=fail_database, config=StationEndpointConfig(observability_url, 4318)
        )
        is None
    )


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


# meiva:note
# End-to-end: with a registered station, run() runs clean on a healthy probe and
# fails open on a failing one.
def test_main_probes_advertised_station_and_does_not_raise(
    unit_home: Path,
    probe_token: str,
    database: Database,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    process_config = snapshot_process_config()
    process_config.set_field("observability_url", "http://10.0.0.9")
    _insert_station_unit(database_gate=database_gate)
    opener = FakeUrlOpener()
    opener.on("http://10.0.0.9:4318", Answer(), Answer(status=503))
    hc.run(
        process_config=process_config,
        database=lambda: database,
        home=unit_home,
        open_url=opener,
    )
    hc.run(
        process_config=process_config,
        database=lambda: database,
        home=unit_home,
        open_url=opener,
    )
    assert opener.urls == ["http://10.0.0.9:4318/v1/traces"] * 2
    assert all(call.headers["Authorization"] == f"Bearer {probe_token}" for call in opener.calls)

    failed_observations = sum(
        "did not answer a bearer OTLP probe" in r["message"] for r in loguru_records
    )
    assert failed_observations == 1
    telemetry_token_path(unit_home).unlink()
    hc.run(
        process_config=process_config,
        database=lambda: database,
        home=unit_home,
        open_url=opener,
    )
    assert len(opener.calls) == 2
    assert any("no published telemetry token" in record["message"] for record in loguru_records)
    assert (
        sum("did not answer a bearer OTLP probe" in r["message"] for r in loguru_records)
        == failed_observations
    )


def test_main_shares_one_captured_gate_with_the_probe_and_log_factory(
    db_url: str,
    unit_home: Path,
    probe_token: str,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    process_config = snapshot_process_config()
    process_config.view.data_plane.db_url = db_url
    process_config.set_field("observability_url", "http://10.0.0.9")
    process_config.set_field("observability_otlp_port", 44318)
    _insert_station_unit(url="http://10.0.0.9:44318", database_gate=database_gate)

    def database() -> Database:
        return Database(
            db_config_from_boot(process_config),
            gate=database_gate,
            direct_url=ClusterDirectUrl(machine_identity.host, home=meiva_home()),
        )

    pipeline = build_pipeline(database=database, machine=machine_identity, home=unit_home)
    try:
        assert _application_name(database()) == database_gate.application_name()
        opener = FakeUrlOpener()
        opener.on("http://10.0.0.9:44318", Answer())
        hc.run(
            process_config=process_config,
            database=database,
            home=unit_home,
            open_url=opener,
        )
        assert len(opener.calls) == 1
        assert opener.calls[0].headers["Authorization"] == f"Bearer {probe_token}"
    finally:
        drain = pipeline.stop(timeout=2)
    assert drain.status is DrainStatus.COMPLETED

    process_config.view.data_plane.db_url = _refused(db_url)
    with pytest.raises(psycopg.OperationalError):
        database().connect()


def test_station_lookup_keeps_the_original_fail_open_boundary() -> None:
    calls = 0

    def fail_database() -> Database:
        nonlocal calls
        calls += 1
        raise RuntimeError("original database failure")

    assert (
        hc.resolve_target(
            database=fail_database, config=StationEndpointConfig("http://station.invalid", 4318)
        )
        is None
    )
    assert calls == 1
