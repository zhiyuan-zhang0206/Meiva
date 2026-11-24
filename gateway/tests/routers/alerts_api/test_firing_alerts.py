from __future__ import annotations

import http.server
import json
import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from gateway.alerts.firing import ACTIVE_ALERTS_PATH, GrafanaSnapshotError, count_firing
from gateway.tests.auth.gateway_config import gateway_test_client

UNAVAILABLE = {
    "state": "unavailable",
    "firing": None,
    "critical": None,
    "error": None,
    "warning": None,
}
NOT_CONFIGURED = {**UNAVAILABLE, "state": "not_configured"}


def _instance(severity: str | None, **labels: str) -> dict[str, object]:
    all_labels = {"alertname": "rule", **labels}
    if severity is not None:
        all_labels["severity"] = severity
    return {"labels": all_labels, "fingerprint": "f", "startsAt": "2026-11-24T00:00:00Z"}


WATCHDOG = _instance("warning", metric="alert_pipeline_watchdog")


def test_the_watchdog_is_not_counted_and_each_severity_has_its_own_count() -> None:
    snapshot = [
        _instance("critical"),
        _instance("error"),
        _instance("error"),
        _instance("warning"),
        WATCHDOG,
    ]

    assert count_firing(snapshot).model_dump() == {
        "state": "ok",
        "firing": 4,
        "critical": 1,
        "error": 2,
        "warning": 1,
    }


@pytest.mark.parametrize(
    ("severity", "counted_as"),
    [
        (None, "warning"),
        ("", "warning"),
        ("info", "warning"),
        ("page", "warning"),
        ("CRITICAL ", "critical"),
        (" Error", "error"),
    ],
)
def test_an_unknown_severity_counts_as_a_warning_and_known_ones_are_normalized(
    severity: str | None, counted_as: str
) -> None:
    counts = count_firing([_instance(severity)]).model_dump()

    assert counts["firing"] == 1
    assert {name: counts[name] for name in ("critical", "error", "warning")} == {
        name: int(name == counted_as) for name in ("critical", "error", "warning")
    }


def test_an_empty_snapshot_is_zero_alerts_that_were_counted() -> None:
    assert count_firing([]).model_dump() == {
        "state": "ok",
        "firing": 0,
        "critical": 0,
        "error": 0,
        "warning": 0,
    }


@pytest.mark.parametrize(
    "payload",
    [{"message": "nope"}, "x", None, [1], [{"fingerprint": "no-labels"}], [{"labels": "x"}]],
)
def test_a_malformed_snapshot_is_refused_whole(payload: object) -> None:
    with pytest.raises(GrafanaSnapshotError):
        count_firing(payload)


@dataclass
class Grafana:
    status: int = 200
    body: bytes = b"[]"
    port: int = 0
    asked: list[tuple[str, dict[str, list[str]]]] = field(
        default_factory=list[tuple[str, dict[str, list[str]]]]
    )

    def answer(self, alerts: list[dict[str, object]]) -> None:
        self.body = json.dumps(alerts).encode()


@pytest.fixture
def grafana() -> Iterator[Grafana]:
    state = Grafana()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            state.asked.append((url.path, parse_qs(url.query)))
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _fields(port: int, *, enabled: bool = True) -> dict[str, object]:
    return {"grafana_proxy_enabled": enabled, "grafana_host": "127.0.0.1", "grafana_port": port}


def _closed_port() -> int:
    with socketserver.TCPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler) as probe:
        return probe.server_address[1]


def test_the_route_counts_what_grafana_reports_firing(grafana: Grafana) -> None:
    grafana.answer([_instance("critical"), _instance("warning"), WATCHDOG])
    with gateway_test_client(fields=_fields(grafana.port)) as client:
        body = client.get("/api/alerts/firing").json()

    assert body == {"state": "ok", "firing": 2, "critical": 1, "error": 0, "warning": 1}
    assert grafana.asked == [
        (ACTIVE_ALERTS_PATH, {"active": ["true"], "silenced": ["false"], "inhibited": ["false"]})
    ]


def test_the_dashboard_carries_the_same_answer(grafana: Grafana) -> None:
    grafana.answer([_instance("error")])
    with gateway_test_client(fields=_fields(grafana.port)) as client:
        dashboard = client.get("/api/stats/dashboard").json()
        firing = client.get("/api/alerts/firing").json()

    assert dashboard["alerts"] == firing
    assert firing == {"state": "ok", "firing": 1, "critical": 0, "error": 1, "warning": 0}


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, b"[]"), (404, b"[]"), (200, b"not json"), (200, b'{"message": "x"}')],
    ids=["server-error", "not-found", "not-json", "not-a-list"],
)
def test_a_grafana_that_answers_badly_is_unavailable_not_zero(
    grafana: Grafana, status: int, body: bytes
) -> None:
    grafana.status, grafana.body = status, body
    with gateway_test_client(fields=_fields(grafana.port)) as client:
        firing = client.get("/api/alerts/firing")
        dashboard = client.get("/api/stats/dashboard")

    assert firing.status_code == 200 and firing.json() == UNAVAILABLE
    assert dashboard.status_code == 200 and dashboard.json()["alerts"] == UNAVAILABLE


def test_a_grafana_that_refuses_connections_is_unavailable() -> None:
    with gateway_test_client(fields=_fields(_closed_port())) as client:
        firing = client.get("/api/alerts/firing")

    assert firing.status_code == 200
    assert firing.json() == UNAVAILABLE


def test_without_a_grafana_upstream_nothing_is_counted(grafana: Grafana) -> None:
    grafana.answer([_instance("critical")])
    with gateway_test_client(fields=_fields(grafana.port, enabled=False)) as client:
        firing = client.get("/api/alerts/firing").json()
        dashboard = client.get("/api/stats/dashboard").json()

    assert firing == NOT_CONFIGURED
    assert dashboard["alerts"] == NOT_CONFIGURED
    assert grafana.asked == []
