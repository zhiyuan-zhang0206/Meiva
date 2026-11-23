from __future__ import annotations

from collections.abc import Callable, Sequence

from google.protobuf.json_format import Parse, ParseError
from google.protobuf.message import DecodeError
from opentelemetry.exporter.otlp.proto.common._internal._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.metrics import Meter, NoOpMeterProvider
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.semconv._incubating.metrics.otel_metrics import (
    create_otel_sdk_exporter_log_exported,
)
from requests import PreparedRequest, RequestException, Response, Session, Timeout

from base.telemetry.delivery.reporting import report_no_pipeline

MAX_LOG_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _LogExportSession(Session):
    def __init__(self, *, monotonic: Callable[[], float], timeout: float) -> None:
        super().__init__()
        self._monotonic = monotonic
        self._timeout = timeout
        self._deadline = 0.0
        self.error_type: str | None = None
        self.rejected = 0

    def start_batch(self) -> None:
        self._deadline = self._monotonic() + self._timeout

    def send(self, request: PreparedRequest, **kwargs: object) -> Response:
        self.error_type = None
        self.rejected = 0
        kwargs["stream"] = True
        kwargs["allow_redirects"] = False
        try:
            remaining = self._deadline - self._monotonic()
            if remaining <= 0:
                raise Timeout("OTLP log export deadline expired")
            requested = kwargs.get("timeout")
            kwargs["timeout"] = (
                min(requested, remaining) if isinstance(requested, int | float) else remaining
            )
            with super().send(request, **kwargs) as response:
                if response.status_code != 200:
                    self.error_type = str(response.status_code)
                else:
                    self._read_acknowledgement(response)
                return response
        except RequestException as exc:
            self.error_type = type(exc).__name__
            raise

    def _read_acknowledgement(self, response: Response) -> None:
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if self._monotonic() >= self._deadline:
                raise Timeout("OTLP log acknowledgement deadline expired")
            if len(payload) + len(chunk) > _MAX_RESPONSE_BYTES:
                self.error_type = "response_too_large"
                return
            payload.extend(chunk)
        if not payload:
            return
        receipt = ExportLogsServiceResponse()
        try:
            if response.headers.get("Content-Type", "").split(";", 1)[0] == "application/json":
                _ = Parse(payload.decode("utf-8"), receipt, ignore_unknown_fields=True)
            else:
                _ = receipt.ParseFromString(bytes(payload))
        except (DecodeError, ParseError, UnicodeDecodeError):
            self.error_type = "invalid_response"
            return
        self.rejected = receipt.partial_success.rejected_log_records


class BoundedHTTPLogExporter(LogRecordExporter):
    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float,
        monotonic: Callable[[], float],
        meter: Meter,
        flush_queue_losses: Callable[[], None],
        max_request_bytes: int = MAX_LOG_REQUEST_BYTES,
    ) -> None:
        self._session = _LogExportSession(monotonic=monotonic, timeout=timeout)
        self._exporter = OTLPLogExporter(
            endpoint=endpoint,
            timeout=timeout,
            session=self._session,
            meter_provider=NoOpMeterProvider(),
        )
        self._exported = create_otel_sdk_exporter_log_exported(meter)
        self._flush_queue_losses = flush_queue_losses
        self._max_request_bytes = max_request_bytes

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self._session.start_batch()
        try:
            return self._export_bounded(batch)
        finally:
            self._flush_queue_losses()

    def _export_bounded(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        if encode_logs(batch).ByteSize() > self._max_request_bytes:
            if len(batch) == 1:
                return self._count(len(batch), "request_too_large")
            midpoint = len(batch) // 2
            left = self._export_bounded(batch[:midpoint])
            right = self._export_bounded(batch[midpoint:])
            return (
                LogRecordExportResult.SUCCESS
                if left is right is LogRecordExportResult.SUCCESS
                else LogRecordExportResult.FAILURE
            )
        self._session.error_type = None
        self._session.rejected = 0
        result = self._exporter.export(batch)
        if self._session.error_type is not None or result is LogRecordExportResult.FAILURE:
            return self._count(len(batch), self._session.error_type or "export_failed")
        rejected = self._session.rejected
        if not 0 <= rejected <= len(batch):
            return self._count(len(batch), "invalid_response")
        _ = self._count(len(batch) - rejected, None)
        return self._count(rejected, "rejected" if rejected else None)

    def _count(self, count: int, error_type: str | None) -> LogRecordExportResult:
        attributes = {"otel.component.type": "otlp_http_log_exporter"}
        if error_type is not None:
            attributes["error.type"] = error_type
        if count:
            self._exported.add(count, attributes)
        if error_type is None:
            return LogRecordExportResult.SUCCESS
        report_no_pipeline(
            "OTLP log export failed for {count} record(s), error.type={error_type}",
            level="error",
            count=count,
            error_type=error_type,
        )
        return LogRecordExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        self._flush_queue_losses()
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()
        self._flush_queue_losses()
