from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from base import telemetry
from base.db import Database
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.metrics import observed_metrics as metrics
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from base.telemetry.tests.otlp.identity_inputs import machine_identity


def test_projection_failure_keeps_jsonl_and_otlp_and_never_emits_recursively(
    tmp_path: Path,
    database: Database,
    db_conn: psycopg.Connection[Any],
    emitted_events: EmittedEvents,
    loguru_records: LogRecords,
) -> None:
    metrics.project_events(database, [])
    log_exporter = InMemoryLogRecordExporter()
    logs = LoggerProvider(shutdown_on_exit=False)
    logs.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    metric_provider = MeterProvider(metric_readers=[InMemoryMetricReader()], shutdown_on_exit=False)
    exporter = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logs, metric_provider),
    )
    event = telemetry.Event(
        ts=datetime(2026, 9, 15, 23, 59, 59, tzinfo=UTC),
        trace_id=None,
        span_id=None,
        agent_id=71,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name="turn_end",
        level="info",
        source="system",
        target_agent_id=None,
        attributes={"ok": True, "duration_seconds": 2.5},
    )
    db_conn.execute("LOCK TABLE agent_metric_observations IN ACCESS EXCLUSIVE MODE")
    with telemetry_otlp.bind_exporter(exporter):
        pipeline = telemetry.build_pipeline(
            database=lambda: database,
            machine=machine_identity(),
            home=Path(".meiva"),
            target_dir=tmp_path,
        )
    try:
        telemetry.emit_prepared(event, producer=lambda: pipeline)
        assert pipeline.flush().status is telemetry.DrainStatus.COMPLETED
    finally:
        db_conn.rollback()
        assert pipeline.stop(timeout=2).status is telemetry.DrainStatus.COMPLETED
    assert len(log_exporter.get_finished_logs()) == 1
    assert len(list(tmp_path.glob("events-*.jsonl"))) >= 1
    assert (
        str(telemetry.event_row(event)["id"]) in next(tmp_path.glob("events-*.jsonl")).read_text()
    )
    assert any("[observed-metrics] projection failed" in msg for msg in loguru_records.messages())
    assert len(emitted_events) == 0, "diagnostics must bypass the emitter"
