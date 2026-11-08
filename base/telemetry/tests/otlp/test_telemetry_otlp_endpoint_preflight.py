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
from pathlib import Path

import pytest

from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_metrics, telemetry_otlp_sdk


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


def test_production_otlp_provider_construction_contract(tmp_path: Path) -> None:
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

    logs, metrics = telemetry_otlp_sdk.build_providers(
        "http://127.0.0.1:4318",
        mirror_dir=tmp_path,
        home=tmp_path,
        instance_id="test-instance",
    )
    try:
        # Both providers must NOT register their own atexit shutdown: it would fire
        # ahead of `base.telemetry.drain_on_exit` (LIFO) and strand every tail
        # record on a shut-down processor (task #4314 triage / #4320).
        assert logs._at_exit_handler is None
        assert metrics._atexit_handler is None
    finally:
        logs.shutdown()
        metrics.shutdown()


# meiva:note
# `service.instance.id` names the process instance, not a single provider build: when the process's
# pipeline builds its OTLP providers a second time (for example after a worker restart), both
# `MeterProvider` resources carry the same id minted once from the root's generator at
# `build_pipeline`. An `OtlpBackend` that builds its own providers without an `instance_id` is
# refused at construction time.
def test_building_providers_twice_in_one_process_preserves_service_instance_id(
    tmp_path: Path,
) -> None:
    import logging
    from types import SimpleNamespace

    from base import telemetry
    from base.clock.ids import new_uuid
    from base.clock.tests.fakes import system_clock
    from base.clock.tests.seeded_rng import seeded_rng
    from base.cluster.machine import MachineFacts
    from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline

    with pytest.raises(ValueError, match="instance_id"):
        telemetry_otlp.OtlpBackend(
            export_allowed=lambda: True,
            enabled=lambda: True,
            endpoint=lambda: "file:///collector",
            mirror_dir=tmp_path,
            home=tmp_path,
            now=system_clock().now,
        )

    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    old_filters = list(sdk_logger.filters)
    pipeline = telemetry.build_pipeline(
        database=object,
        machine=MachineFacts.of(name="test-runner", role=frozenset({"agent-runner"})),
        home=tmp_path,
        now=system_clock().now,
        otlp_enabled=lambda: True,
        otlp_endpoint=lambda: "file:///collector",
        rng=seeded_rng(7),
    )
    assert isinstance(pipeline, ExportingEventPipeline)
    backend = pipeline._exporter
    first = SimpleNamespace(logs=None, metrics=None, metric_reader=None)
    second = SimpleNamespace(logs=None, metrics=None, metric_reader=None)
    try:
        assert backend._initialize_worker(first)  # pyright: ignore[reportArgumentType]
        assert backend._initialize_worker(second)  # pyright: ignore[reportArgumentType]
        assert first.metrics is not None and second.metrics is not None
        assert first.metrics is not second.metrics
        first_id = first.metrics._sdk_config.resource.attributes["service.instance.id"]
        second_id = second.metrics._sdk_config.resource.attributes["service.instance.id"]
        assert first_id == second_id == str(new_uuid(seeded_rng(7)))
    finally:
        sdk_logger.filters[:] = old_filters
        for worker in (first, second):
            if worker.logs is not None:
                worker.logs.shutdown()
            if worker.metrics is not None:
                worker.metrics.shutdown()
        pipeline.stop(timeout=2)
