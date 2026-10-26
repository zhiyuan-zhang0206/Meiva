"""Native readiness and Loki write/read protocol tests."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineFacts, MachineRoleInvalid
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.faults import InvalidRoleIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths.tests.homes import unit_home as unit_home
from base.telemetry.lgtm_local import backend_urls
from services.supervision.healthchecks import lgtm as hc
from services.supervision.healthchecks.tests.loki_stub import BackendStub


def test_the_default_backend_urls_dial_the_historical_loopback_ports() -> None:
    # meiva:note
    # `lgtm_grafana_listen_host` defaults to the wildcard; the probe must dial loopback.
    assert backend_urls() == {
        "loki": "http://127.0.0.1:3100",
        "prometheus": "http://127.0.0.1:9090",
        "grafana": "http://127.0.0.1:3003",
    }


_NO_PAUSE = (0.0, 0.0)
_CLOCK = Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=SYSTEM_TIMERS)


def test_write_path_probe_pauses_on_the_clock_it_was_given_between_throttled_pushes() -> None:
    timers = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=timers)
    with BackendStub() as loki:
        loki.script.push_answers = [(429, b""), (429, b""), (204, b"")]

        assert hc.write_path_probe(loki.url, clock=clock, backoff_s=(1.0, 2.0)) == (True, "ok")
        assert len(loki.script.pushes) == 3
    assert timers.sleeps == [1.0, 2.0]


def test_probe_backend_asks_the_readiness_path_of_the_named_backend() -> None:
    with BackendStub() as loki:
        assert hc.probe_backend("loki", urls={"loki": loki.url}).alive
        loki.script.ready = (503, b"")
        assert not hc.probe_backend("loki", urls={"loki": loki.url}).alive


def test_probe_backend_without_an_explicit_port_is_unavailable() -> None:
    probe = hc.probe_backend("loki", urls={"loki": "http://127.0.0.1"})
    assert not probe.alive
    assert "no explicit local port" in probe.detail


def test_write_path_probe_rejects_400_push() -> None:
    with BackendStub() as loki:
        loki.script.push_answers = [(400, b"")]

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "push_http_400",
        )


def test_write_path_probe_retries_429_with_bounded_backoff() -> None:
    with BackendStub() as loki:
        loki.script.push_answers = [(429, b"")]

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "push_http_429",
        )
        assert len(loki.script.pushes) == 3


def test_write_path_probe_succeeds_after_a_throttle() -> None:
    with BackendStub() as loki:
        loki.script.push_answers = [(429, b""), (204, b"")]

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (True, "ok")
        assert len(loki.script.pushes) == 2


def test_write_path_probe_identifies_stuck_ingester() -> None:
    with BackendStub() as loki:
        loki.script.push_answers = [
            (500, b"RPC error: code = Unknown desc = InGeStEr Is ShUtTiNg DoWn")
        ]

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "ingester_shutting_down",
        )


def test_write_path_probe_does_not_misclassify_plain_503() -> None:
    with BackendStub() as loki:
        loki.script.push_answers = [(503, b"write throttled because disk usage is too high")]

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "push_http_503",
        )


def test_write_path_probe_reports_push_request_error() -> None:
    with BackendStub() as loki:
        url = loki.url

    assert hc.write_path_probe(url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (False, "push_error")


def test_write_path_probe_reports_marker_not_visible() -> None:
    with BackendStub() as loki:
        loki.script.query_mode = "empty"

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "probe_not_visible",
        )


def test_write_path_probe_finds_marker_in_numeric_query_window() -> None:
    with BackendStub() as loki:
        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (True, "ok")

        push = loki.script.pushes[0]
        assert push["path"] == "/otlp/v1/logs"
        assert push["org"] == "fake"
        assert push["content_type"] == "application/json"
        resource_log = push["payload"]["resourceLogs"][0]
        attributes = {
            attribute["key"]: attribute["value"]["stringValue"]
            for attribute in resource_log["resource"]["attributes"]
        }
        record = resource_log["scopeLogs"][0]["logRecords"][0]
        marker = record["body"]["stringValue"]
        assert attributes == {"agent_id": "watchdog", "event_name": "watchdog-write-probe"}
        assert record["timeUnixNano"].isdigit()
        assert marker == f"watchdog-write-probe-{record['timeUnixNano']}"
        assert record["timeUnixNano"] == marker.removeprefix("watchdog-write-probe-")
        (request_path,) = loki.script.queries
        assert request_path.startswith("/loki/api/v1/query_range?")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request_path).query)
        assert query["query"] == [
            f'{{agent_id="watchdog", event_name="watchdog-write-probe"}} |= "{marker}"'
        ]
        marker_ts = int(marker.removeprefix("watchdog-write-probe-"))
        assert int(query["end"][0]) > marker_ts  # range end is exclusive
        assert int(query["end"][0]) - int(query["start"][0]) >= 121_000_000_000


def test_write_path_probe_reports_query_request_error() -> None:
    with BackendStub() as loki:
        loki.script.query_mode = "drop"

        assert hc.write_path_probe(loki.url, clock=_CLOCK, backoff_s=_NO_PAUSE) == (
            False,
            "query_error",
        )


def test_is_lgtm_host_accepts_station_capability(tmp_path: Path) -> None:
    """The declarative observability-station capability designates the host
    exactly like the marker — the watchdog keepalive and `meiva status` gate on
    either form."""
    home = tmp_path / "station"
    home.mkdir()

    # No marker, no capability -> not the station.
    assert hc.is_lgtm_host(home, roles=frozenset({"gateway"})) is False
    assert hc.is_lgtm_host(home, roles=None) is False

    # Capability form.
    assert hc.is_lgtm_host(home, roles=frozenset({"observability-station"})) is True

    # Marker form.
    (home / "lgtm-host").touch()
    assert hc.is_lgtm_host(home, roles=None) is True
    assert hc.is_lgtm_host(home, roles=frozenset({"gateway"})) is True


@pytest.mark.parametrize("status", [301, 401, 500, 503])
def test_unready_http_cannot_certify_backend(status: int) -> None:
    with BackendStub() as backend:
        backend.script.ready = (status, b"")

        assert not hc.readiness_at("loki", f"{backend.url}/ready").alive


def test_a_ready_backend_is_certified() -> None:
    with BackendStub() as backend:
        assert hc.readiness_at("loki", f"{backend.url}/ready").alive


@pytest.mark.parametrize("database,ready", [("ok", True), ("failed", False)])
def test_grafana_health_requires_database_ready(database: str, ready: bool) -> None:
    with BackendStub() as backend:
        backend.script.ready = (200, json.dumps({"database": database}).encode())

        assert hc.readiness_at("grafana", f"{backend.url}/ready").alive is ready


def test_an_unreachable_backend_reads_down() -> None:
    with BackendStub() as backend:
        url = f"{backend.url}/ready"

    assert not hc.readiness_at("loki", url).alive


def test_the_roles_lgtm_decides_on_are_none_only_while_no_capability_is_configured(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    unconfigured = MachineFacts.of(name="m", role=())
    invalid = InvalidRoleIdentity(MachineFacts.of(name="m", role="gateway"))

    assert hc.lgtm_roles(unconfigured) is None
    with pytest.raises(MachineRoleInvalid, match="fixed test fault"):
        hc.lgtm_roles(invalid)
    machine_identity.set(role="observability-station")
    assert hc.lgtm_roles(machine_identity) == frozenset({"observability-station"})
