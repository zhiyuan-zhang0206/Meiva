"""Telemetry otlp cases: backend init failure isolated and waits before."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from base import telemetry
from base.db import Database
from base.telemetry.delivery import loss
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tests.otlp.test_telemetry_otlp import AGENT, collected_metrics, otlp_test_event
from base.telemetry.tests.otlp.test_telemetry_otlp import (
    bind_telemetry as bind_telemetry,
)
from base.telemetry.tests.otlp.test_telemetry_otlp import (
    otlp_backend as otlp_backend,
)


class _NoopProvider:
    def shutdown(self) -> None:
        pass

    def force_flush(self, *, timeout_millis: int = 30000) -> bool:
        return True


class _FailingMetricsProvider(_NoopProvider):
    def __init__(self, attempts: list[int], *, fail_times: int | None = None, message: str) -> None:
        self._attempts = attempts
        self._fail_times = fail_times
        self._message = message

    def get_meter(self, _name: str) -> object:
        self._attempts.append(len(self._attempts) + 1)
        if self._fail_times is None or len(self._attempts) <= self._fail_times:
            raise RuntimeError(self._message)
        return object()


class _BlockedLogExporter(LogRecordExporter):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def export(self, batch: Any) -> Any:
        self._entered.set()
        assert self._release.wait(5)
        return LogRecordExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def test_backend_init_failure_isolated_and_waits_before_retry() -> None:
    """A backend that cannot come up (bad endpoint / missing dep) never raises
    into the emitter and does not retry again inside the five-minute window."""
    attempts: list[int] = []
    logs = _NoopProvider()
    metrics = _FailingMetricsProvider(attempts, message="collector unreachable")
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        endpoint="http://127.0.0.1:4318",
        providers=(logs, metrics),
    )
    try:
        assert backend.export_batch([otlp_test_event()]) is None  # does not raise
        assert backend._logs is None
        assert backend.export_batch([otlp_test_event()]) is None  # no per-batch retry
        assert attempts == [1]
        assert backend._init_failed_at is not None
    finally:
        backend.shutdown()


def test_backend_retry_recovers_and_emits_real_status_events(
    emitted_events: EmittedEvents,
) -> None:
    """A failed init reports into the surviving mirror, then a later retry
    initializes the backend and reports recovery."""
    attempts: list[int] = []
    logs = _NoopProvider()
    metrics = _FailingMetricsProvider(attempts, fail_times=1, message="collector booting")
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        endpoint="http://127.0.0.1:4318",
        retry_interval_s=0.05,
        providers=(logs, metrics),
    )
    try:
        assert backend._ensure() is False
        disabled = emitted_events.named("otlp_backend_disabled")
        assert len(disabled) == 1
        assert disabled[0].attributes == {
            "reason": "init failed: RuntimeError('collector booting')",
            "endpoint": "http://127.0.0.1:4318",
        }

        assert backend._ensure() is False
        assert attempts == [1]

        time.sleep(0.06)
        assert backend._ensure() is True
        assert backend._logs is logs
        recovered = emitted_events.named("otlp_backend_recovered")
        assert len(recovered) == 1
        assert recovered[0].attributes["endpoint"] == "http://127.0.0.1:4318"
        assert recovered[0].attributes["disabled_s"] >= 0.05
        assert backend._init_failed_at is None
    finally:
        backend.shutdown()


def test_backend_unreachable_probe_emits_specific_disabled_reason(
    emitted_events: EmittedEvents,
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        endpoint=endpoint,
    )
    try:
        assert backend._ensure() is False
        disabled = emitted_events.named("otlp_backend_disabled")
        assert len(disabled) == 1
        assert disabled[0].attributes == {
            "reason": "endpoint not answering",
            "endpoint": endpoint,
        }
    finally:
        backend.shutdown()


def test_queue_full_sheds_counted_not_blocking() -> None:
    """The bounded queue sheds instead of blocking the caller when the worker
    cannot keep up — the isolation contract for a hung OTLP endpoint."""
    entered, release = threading.Event(), threading.Event()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(_BlockedLogExporter(entered, release))
    )
    metric_provider = MeterProvider(metric_readers=[InMemoryMetricReader()])
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logger_provider, metric_provider),
        queue_maxsize=1,
    )
    try:
        backend.export_batch([otlp_test_event()])
        assert entered.wait(2)
        backend.export_batch([otlp_test_event()])
        backend.export_batch([otlp_test_event()])
        assert backend._dropped == 1
        assert backend._queue.qsize() == 1
    finally:
        release.set()
        backend.shutdown()


def test_pipeline_exports_to_otlp(otlp_backend: Any) -> None:
    """One emit lands the OTLP copies (log record + metric) through the real
    drain thread. The Postgres copy was retired with the LGTM cutover (task
    #1197) — OTLP + the JSONL mirror are the sinks."""
    _backend, log_exporter, metric_reader = otlp_backend
    telemetry.emit(
        "telemetry",
        "llm_usage",
        agent_id=AGENT,
        attributes={"model": "m", "in_total": 7, "latency_ms": 1.5},
    )
    telemetry.sync()

    # OTLP copies — the worker thread is async, poll (repo convention: the
    # emitter's own drain is async too; a flush can race it).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not log_exporter.get_finished_logs():  # pyright: ignore[reportUnknownMemberType]
        time.sleep(0.05)
    records = log_exporter.get_finished_logs()  # pyright: ignore[reportUnknownMemberType]
    assert len(records) == 1  # pyright: ignore[reportUnknownArgumentType]
    attrs = records[0].log_record.attributes  # pyright: ignore[reportUnknownMemberType]
    assert attrs is not None
    assert attrs["event_name"] == "llm_usage"

    metrics = collected_metrics(metric_reader)
    assert metrics["meiva_llm_usage_in_total"].data.data_points[0].value == 7
    assert metrics["meiva_llm_usage_latency"].data.data_points[0].sum == 1.5


def test_pipeline_mirror_survives_otlp_failure(database: Database, tmp_path: Path) -> None:
    """A broken OTLP backend never blocks the drain — the JSONL mirror still
    holds the batch (the PG copy is gone, task #1197)."""
    attempts: list[int] = []
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(
            _NoopProvider(),
            _FailingMetricsProvider(attempts, message="collector unreachable"),
        ),
    )
    existing = telemetry.process_state["pipeline"]
    if existing is not None:
        existing.stop(timeout=2)
    with telemetry_otlp.bind_exporter(backend):
        pipeline = telemetry.build_pipeline(
            database=lambda: database,
            machine=machine_identity(),
            home=Path(".meiva"),
            target_dir=tmp_path,
        )
    try:
        telemetry.init_telemetry(process="test-proc", pipeline=pipeline)
        telemetry.emit("log", "log", agent_id=AGENT, attributes={"msg": "boom"})
        telemetry.sync()
    finally:
        pipeline.stop(timeout=2)

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = tmp_path / f"events-{day}.jsonl"
    assert path.exists()
    assert any(
        '"event_name":"log"' in line and '"msg":"boom"' in line
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def test_handshake_env_no_longer_gates_export() -> None:
    """The MEIVA_EXEC_REQUEST_FILE handshake was the old child-export gate; it
    must no longer disable OTLP — the export flag is the only authority."""
    assert (
        telemetry_otlp.OtlpBackend(
            export_allowed=lambda: True,
            enabled=lambda: True,
        )._enabled()
        is True
    )


def test_flush_force_flushes_sdk_batch_records() -> None:
    """flush() must force-flush the SDK batch processor: a short-lived exec
    child exits before the 5s batch window fires on its own, so without the
    force_flush the app-level queue drain is not enough to reach the wire."""
    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    metric_provider = MeterProvider(metric_readers=[InMemoryMetricReader()])
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logger_provider, metric_provider),
    )

    try:
        backend.export_batch([otlp_test_event(attributes={"fn": "files.read"})])
        # Without force_flush the in-memory batch exporter would stay empty
        # until its 5s schedule; the flush must surface the record now.
        backend.flush()
        assert len(log_exporter.get_finished_logs()) == 1
    finally:
        backend.shutdown()


def test_warmup_builds_backend_when_enabled() -> None:
    """warmup() constructs the OTel providers so a short-lived child never
    first builds them during interpreter shutdown."""
    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(
            logger_provider,
            MeterProvider(metric_readers=[InMemoryMetricReader()]),
        ),
    )

    try:
        backend.warmup()
        assert backend._logs is not None
    finally:
        backend.shutdown()


def test_queue_loss_metric_survives_full_log_lane(tmp_path: Path) -> None:
    entered, release = threading.Event(), threading.Event()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(_BlockedLogExporter(entered, release))
    )
    reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[reader])
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logger_provider, metric_provider),
        queue_maxsize=1,
        mirror_dir=tmp_path,
    )
    try:
        backend.export_batch([otlp_test_event()])
        assert entered.wait(2)
        backend.export_batch([otlp_test_event()])
        backend.export_batch([otlp_test_event(), otlp_test_event()])
        day = datetime.now(UTC).strftime("%Y%m%d")
        mirror_path = tmp_path / f"events-{day}.jsonl"
        assert mirror_path.exists()
        rows = [json.loads(line) for line in mirror_path.read_text(encoding="utf-8").splitlines()]
        drop_rows = [row for row in rows if row["event_name"] == "event_log_drop"]
        assert len(drop_rows) == 1
        assert drop_rows[0]["attributes"]["n"] == 2
        metrics = collected_metrics(reader)
        assert metrics["meiva_event_log_drop_n"].data.data_points[0].value == 2
        assert metrics["meiva_event_log_drop_last_dropped_at"].data.data_points[0].value > 0
    finally:
        release.set()
        backend.shutdown()


def test_real_otel_sdk_queue_overflow_is_observed_without_log_recursion(
    tmp_path: Path,
) -> None:
    entered, release = threading.Event(), threading.Event()
    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    old_filters = list(sdk_logger.filters)
    sdk_logger.filters[:] = []
    loss.install_exporter_drop_observer(mirror_dir=tmp_path)
    provider = LoggerProvider()
    provider.add_log_record_processor(
        BatchLogRecordProcessor(
            _BlockedLogExporter(entered, release),
            max_queue_size=1,
            max_export_batch_size=1,
            schedule_delay_millis=60000,
        )
    )
    emitter = provider.get_logger("test")
    try:
        emitter.emit(LogRecord(body="first"))
        assert entered.wait(2)
        for _ in range(3):
            emitter.emit(LogRecord(body="more"))
        day = datetime.now(UTC).strftime("%Y%m%d")
        mirror_path = tmp_path / f"events-{day}.jsonl"
        assert mirror_path.exists()
        reports = [
            json.loads(line) for line in mirror_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(reports) == 2
        assert all(
            row["attributes"]["queue"] == "otel-sdk" and row["level"] == "error" for row in reports
        )
    finally:
        release.set()
        provider.shutdown()
        sdk_logger.filters[:] = old_filters


def test_delayed_loss_summary_cannot_rewind_newer_loss_metric(otlp_backend: Any) -> None:
    backend, _logs, reader = otlp_backend
    assert backend._ensure()
    for timestamp in (2000.0, 1000.0):
        backend._record_metrics(
            otlp_test_event(
                category="telemetry",
                event_name="event_log_drop",
                attributes={"n": 1, "queue": "emitter", "last_dropped_at": timestamp},
            )
        )
    metrics = collected_metrics(reader)
    assert metrics["meiva_event_log_drop_last_dropped_at"].data.data_points[0].value == 2000.0
    assert metrics["meiva_event_log_drop_n"].data.data_points[0].value == 2
