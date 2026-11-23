from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExportResult
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, Sum
from opentelemetry.sdk.resources import Resource

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.otlp.http_log_exporter import BoundedHTTPLogExporter

_RECEIVER_LIMIT = 20 * 1024 * 1024


@dataclass
class Receiver:
    endpoint: str = ""
    status: int = 200
    rejected: int = 0
    lengths: list[int] = field(default_factory=list)
    batches: list[ExportLogsServiceRequest] = field(default_factory=list)


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    inbox = Receiver()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            inbox.lengths.append(length)
            body = self.rfile.read(length)
            if length > _RECEIVER_LIMIT:
                self.send_error(400)
                return
            inbox.batches.append(ExportLogsServiceRequest.FromString(body))
            reply = ExportLogsServiceResponse()
            if inbox.rejected:
                reply.partial_success.rejected_log_records = inbox.rejected
            payload = reply.SerializeToString()
            self.send_response(inbox.status)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        inbox.endpoint = f"http://127.0.0.1:{server.server_port}/v1/logs"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield inbox
        finally:
            server.shutdown()
            thread.join()


def _record(body: str, *, severity: str = "INFO") -> ReadableLogRecord:
    return ReadableLogRecord(
        log_record=LogRecord(body=body, severity_text=severity),
        resource=Resource({"service.name": "log-wire-test"}),
    )


def _counts(reader: InMemoryMetricReader) -> dict[object, float]:
    data = reader.get_metrics_data()
    assert data is not None
    return {
        (point.attributes or {}).get("error.type"): point.value
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "otel.sdk.exporter.log.exported" and isinstance(metric.data, Sum)
        for point in metric.data.data_points
    }


@pytest.fixture
def exporter(
    receiver: Receiver,
) -> Iterator[tuple[BoundedHTTPLogExporter, InMemoryMetricReader]]:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    exporter = BoundedHTTPLogExporter(
        receiver.endpoint,
        timeout=3.5,
        monotonic=time.monotonic,
        meter=meter_provider.get_meter("test"),
        flush_queue_losses=lambda: None,
    )
    try:
        yield exporter, reader
    finally:
        exporter.shutdown()
        meter_provider.shutdown()


def test_large_batch_preserves_warning_below_receiver_limit(
    receiver: Receiver, exporter: tuple[BoundedHTTPLogExporter, InMemoryMetricReader]
) -> None:
    output, reader = exporter
    records = [_record("x" * 62_000)] * 511 + [_record("retained warning", severity="WARNING")]
    assert output.export(records) is LogRecordExportResult.SUCCESS
    assert len(receiver.lengths) > 1
    assert max(receiver.lengths) < _RECEIVER_LIMIT
    logs = [
        record
        for batch in receiver.batches
        for resource in batch.resource_logs
        for scope in resource.scope_logs
        for record in scope.log_records
    ]
    assert len(logs) == 512
    assert (logs[-1].body.string_value, logs[-1].severity_text) == ("retained warning", "WARNING")
    assert _counts(reader) == {None: 512}


@pytest.mark.parametrize(
    ("status", "rejected", "counts"),
    [(400, 0, {"400": 3}), (200, 2, {None: 1, "rejected": 2})],
)
def test_rejected_records_never_count_as_success_or_retry(
    receiver: Receiver,
    exporter: tuple[BoundedHTTPLogExporter, InMemoryMetricReader],
    loguru_records: LogRecords,
    status: int,
    rejected: int,
    counts: dict[object, float],
) -> None:
    receiver.status = status
    receiver.rejected = rejected
    output, reader = exporter
    assert output.export([_record("sample")] * 3) is LogRecordExportResult.FAILURE
    assert len(receiver.batches) == 1
    assert _counts(reader) == counts
    diagnostics = loguru_records.extras(level="ERROR", message="OTLP log export failed for")
    assert len(diagnostics) == 1
    assert diagnostics[0]["_no_emitter"] is True
    assert diagnostics[0]["count"] == (rejected or 3)
