"""Endpoint preflight (2026-08-12 prod incident) + provider construction contract.

Split out of test_telemetry_otlp.py to stay under the structure-lint's
per-file line budget.

The preflight must treat ANY HTTP answer as "collector up": the OTLP receiver
answers /v1/logs with 415 unless the body carries an OTLP content type, and
urlopen raises HTTPError on 4xx/5xx. The original probe sent no Content-Type
(urllib defaults to application/x-www-form-urlencoded), misread the 415 as
"no collector", and silently disabled OTLP export for every process that
restarted after the #1214 rollout — no events in Loki, no meiva_* metrics.
"""

from __future__ import annotations

import http.server
import socket
import threading

import pytest

from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_metrics


class _ProbeHandler(http.server.BaseHTTPRequestHandler):
    """HTTP server answering POST /v1/logs with a configurable status."""

    status = 200

    def do_POST(self) -> None:
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def probe_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_endpoint_reachable_accepts_any_http_status(
    probe_server: http.server.HTTPServer,
):
    port = probe_server.server_address[1]
    endpoint = f"http://127.0.0.1:{port}"
    reachable = telemetry_otlp.OtlpBackend._endpoint_reachable

    _ProbeHandler.status = 200
    assert reachable(endpoint) is True

    # A 415 (content-type rejection) still proves a listener is up — the
    # 2026-08-12 incident class: the probe misread it as "no collector".
    _ProbeHandler.status = 415
    assert reachable(endpoint) is True

    _ProbeHandler.status = 405
    assert reachable(endpoint) is True


def test_endpoint_reachable_connection_refused_is_false():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert telemetry_otlp.OtlpBackend._endpoint_reachable(f"http://127.0.0.1:{port}") is False


def test_endpoint_reachable_non_http_scheme_skips_probe():
    assert telemetry_otlp.OtlpBackend._endpoint_reachable("file:///tmp/x") is True


def test_production_otlp_provider_construction_contract() -> None:
    """Log and metric SDK workers receive the same explicit HTTP deadline, and
    both providers leave the process-exit shutdown to the emitter (task #4320)."""
    plan = telemetry_otlp_metrics.provider_build_plan("http://127.0.0.1:4318")
    timeout_s = telemetry_otlp_metrics.OTLP_HTTP_TIMEOUT_S

    assert plan.logs_endpoint == "http://127.0.0.1:4318/v1/logs"
    assert plan.metrics_endpoint == "http://127.0.0.1:4318/v1/metrics"
    assert plan.http_timeout_s == timeout_s
    assert plan.export_timeout_millis == timeout_s * 1000
    assert 0 < timeout_s <= 5.0
    assert plan.shutdown_on_exit is False

    logs, metrics = telemetry_otlp_metrics.build_providers("http://127.0.0.1:4318")
    try:
        # Both providers must NOT register their own atexit shutdown: it would fire
        # ahead of `base.telemetry.drain_on_exit` (LIFO) and strand every tail
        # record on a shut-down processor (task #4314 triage / #4320).
        assert logs._at_exit_handler is None
        assert metrics._atexit_handler is None
    finally:
        logs.shutdown()
        metrics.shutdown()
