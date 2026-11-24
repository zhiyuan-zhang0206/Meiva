from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from opentelemetry.sdk._logs import LoggerProvider, ReadableLogRecord
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from base.telemetry.otlp.telemetry_otlp import OtlpBackend

__all__ = ["recording_backend"]


class _SinkExporter(LogRecordExporter):
    def __init__(self, sink: Callable[[dict[str, Any]], None]) -> None:
        self._sink = sink

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        for item in batch:
            body = json.loads(str(item.log_record.body))
            self._sink(
                {key: body[key] for key in ("event_name", "source", "agent_id", "attributes")}
            )
        return LogRecordExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


def recording_backend(
    *, sink: Callable[[dict[str, Any]], None], home: Path, now: Callable[[], datetime]
) -> OtlpBackend:
    logs = LoggerProvider(shutdown_on_exit=False)
    logs.add_log_record_processor(SimpleLogRecordProcessor(_SinkExporter(sink)))
    metrics = MeterProvider(metric_readers=[InMemoryMetricReader()], shutdown_on_exit=False)
    return OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logs, metrics),
        endpoint=lambda: "http://127.0.0.1:4318",
        home=home,
        now=now,
    )
