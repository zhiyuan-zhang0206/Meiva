"""Collector health uses the OTLP protocol response."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from base.cluster.machine import (
    MachineFacts,
    MachineRoleInvalid,
)
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.faults import InvalidRoleIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from services.supervision.healthchecks import otel_collector as hc
from services.supervision.healthchecks.plans.collector_metrics import CollectorPressure


# meiva:note
# A real HTTP listener on port 0 standing in for the collector: its OTLP receiver answers a
# trace POST with `trace_status`, its self-metrics endpoint serves `metrics`; `seen` records
# every request as `(method, path, content_type, body)`.
class _Collector:
    def __init__(self, *, trace_status: int = 200, metrics: bytes = b"") -> None:
        self.seen: list[tuple[str, str, str | None, bytes]] = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                collector.seen.append(
                    ("POST", self.path, self.headers.get("Content-Type"), self.rfile.read(length))
                )
                self._reply(trace_status)

            def do_GET(self) -> None:
                collector.seen.append(("GET", self.path, None, b""))
                self._reply(200, metrics)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_port
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def _collector(*, trace_status: int = 200, metrics: bytes = b"") -> Generator[_Collector]:
    collector = _Collector(trace_status=trace_status, metrics=metrics)
    try:
        yield collector
    finally:
        collector.close()


def _closed_port() -> int:
    with _collector() as collector:
        return collector.port


def test_collector_health_is_the_otlp_answer() -> None:
    with _collector() as collector:
        assert hc.probe_collector_at(collector.port).alive is True
    assert hc.probe_collector_at(_closed_port()).alive is False


def test_collector_serves_this_home_fails_closed_without_machine_role(tmp_path: Path) -> None:
    missing = MachineFacts.of(name="collector", role=())
    invalid = InvalidRoleIdentity(MachineFacts.of(name="collector", role="agent-runner"))

    assert (
        hc.collector_serves_this_home(machine=missing, home=tmp_path, endpoint_explicit=False)
        is False
    )
    with pytest.raises(MachineRoleInvalid, match="fixed test fault"):
        hc.collector_serves_this_home(machine=invalid, home=tmp_path, endpoint_explicit=False)


def test_collector_serves_this_home_keeps_runner_behavior(
    tmp_path: Path, *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role=frozenset({"agent-runner"}))

    assert (
        hc.collector_serves_this_home(
            machine=machine_identity, home=tmp_path, endpoint_explicit=False
        )
        is True
    )


def test_collector_serves_this_home_requires_gateway_lgtm_marker(
    tmp_path: Path, *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(role=frozenset({"gateway", "agent-runner"}))

    assert not hc.collector_serves_this_home(
        machine=machine_identity, home=tmp_path, endpoint_explicit=False
    )
    (tmp_path / "lgtm-host").touch()
    assert hc.collector_serves_this_home(
        machine=machine_identity, home=tmp_path, endpoint_explicit=False
    )


def test_collector_serves_this_home_with_explicit_endpoint_skips_marker(
    tmp_path: Path, *, machine_identity: ConfiguredMachineIdentity
) -> None:
    """An explicit MEIVA_TELEMETRY_OTLP_ENDPOINT opens the healthcheck's
    collector responsibility on a non-LGTM gateway (marker OR override)."""
    machine_identity.set(role=frozenset({"gateway", "agent-runner"}))

    assert hc.collector_serves_this_home(
        machine=machine_identity, home=tmp_path, endpoint_explicit=True
    )


# meiva:note
# A 2xx proves the receiver parsed and accepted a valid OTLP request.
def test_is_alive_ok_sends_a_valid_otlp_trace_request() -> None:
    with _collector() as collector:
        assert hc.is_alive(collector.port) is True

        assert collector.seen == [
            ("POST", "/v1/traces", "application/json", b'{"resourceSpans":[]}')
        ]


def test_is_alive_rejecting_valid_otlp_is_not_healthy() -> None:
    """A listener that rejects the collector's valid OTLP body is not a healthy
    ingestion pipeline; a bare socket answer is insufficient."""
    with _collector(trace_status=415) as collector:
        assert hc.is_alive(collector.port) is False


def test_is_alive_connection_refused() -> None:
    """Connection-level failure — the sidecar is down."""
    assert hc.is_alive(_closed_port()) is False


# meiva:note
# The watchdog probes the same per-unit endpoint that converge renders.
def test_queue_pressure_reads_the_self_metrics_of_the_given_port() -> None:
    with _collector() as collector:
        assert hc.queue_pressure(collector.port) == CollectorPressure(
            saturated=(), enqueue_failures=()
        )
        assert collector.seen == [("GET", "/metrics", None, b"")]


def test_queue_pressure_is_unknown_when_the_endpoint_is_down() -> None:
    assert hc.queue_pressure(_closed_port()) is None


def test_queue_pressure_reports_full_queue_and_drop_counter() -> None:
    """Collector internal metrics make the exact queue and rejected-item count
    visible; the healthcheck does not infer pressure from process liveness."""
    payload = b"""\
otelcol_exporter_queue_capacity{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_queue_size{data_type="metrics",exporter="otlphttp/prometheus"} 1000
otelcol_exporter_enqueue_failed_metric_points{exporter="otlphttp/prometheus"} 78336
otelcol_exporter_queue_capacity{data_type="logs",exporter="otlphttp/loki"} 5000
otelcol_exporter_queue_size{data_type="logs",exporter="otlphttp/loki"} 12
"""
    with _collector(metrics=payload) as collector:
        pressure = hc.queue_pressure(collector.port)

    assert pressure is not None
    assert pressure.saturated == ("otlphttp/prometheus",)
    assert pressure.enqueue_failures == (("otlphttp/prometheus", 78336),)
