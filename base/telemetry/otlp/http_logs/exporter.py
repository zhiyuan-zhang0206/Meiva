from __future__ import annotations

from collections.abc import Buffer, Callable, Iterable, Sequence
from typing import Protocol

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
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool, PoolManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.util import Timeout as SocketTimeout

from base.host.boundary.untyped import object_of
from base.telemetry.delivery.reporting import report_no_pipeline
from base.telemetry.otlp.http_logs.deadline import LogExportDeadline, LogExportSocketWatcher

MAX_LOG_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


type _SocketOutput = _Readable | Iterable[Buffer] | Buffer | str


class _DeadlineAdapter(HTTPAdapter):
    def __init__(self, watcher: LogExportSocketWatcher) -> None:
        self._watcher = watcher
        super().__init__()
        self._watch_connections(self.poolmanager)

    def _watch_connections(self, manager: PoolManager) -> None:
        watcher = self._watcher

        def watch(connection: HTTPConnection) -> None:
            if connection.sock is None:
                connection.connect()
            if connection.sock is not None:
                watcher.watch_socket(connection.sock)

        class DeadlineHTTPConnection(HTTPConnection):
            def send(self, data: _SocketOutput) -> None:
                watch(self)
                super().send(data)

        class DeadlineHTTPSConnection(HTTPSConnection):
            def send(self, data: _SocketOutput) -> None:
                watch(self)
                super().send(data)

        class DeadlineHTTPPool(HTTPConnectionPool):
            ConnectionCls = DeadlineHTTPConnection  # pyright: ignore[reportAssignmentType]

        class DeadlineHTTPSPool(HTTPSConnectionPool):
            ConnectionCls = DeadlineHTTPSConnection  # pyright: ignore[reportAssignmentType]

        pools: dict[str, type[HTTPConnectionPool] | type[HTTPSConnectionPool]] = {
            "http": DeadlineHTTPPool,
            "https": DeadlineHTTPSPool,
        }
        manager.pool_classes_by_scheme = pools

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: object) -> PoolManager:
        manager = object_of(super().proxy_manager_for, proxy, **proxy_kwargs)
        if not isinstance(manager, PoolManager):
            raise TypeError("OTLP HTTP proxy manager must be a urllib3 PoolManager")
        self._watch_connections(manager)
        return manager


class _LogExportSession(Session):
    def __init__(
        self,
        *,
        monotonic: Callable[[], float],
        timeout: float,
        deadline: LogExportDeadline,
        watcher: LogExportSocketWatcher,
    ) -> None:
        super().__init__()
        self._monotonic = monotonic
        self._timeout = timeout
        self._batch_until = 0.0
        self._deadline = deadline
        self._watcher = watcher
        adapter = _DeadlineAdapter(watcher)
        self.mount("http://", adapter)
        self.mount("https://", adapter)
        self.error_type: str | None = None
        self.rejected = 0

    def start_batch(self) -> None:
        self._batch_until = self._monotonic() + self._timeout

    def batch_expired(self) -> bool:
        return self._deadline.remaining(self._batch_until) <= 0

    def send(self, request: PreparedRequest, **kwargs: object) -> Response:
        self.error_type = None
        self.rejected = 0
        kwargs["stream"] = True
        kwargs["allow_redirects"] = False
        try:
            remaining = self._deadline.remaining(self._batch_until)
            if remaining <= 0:
                raise Timeout("OTLP log export deadline expired")
            requested = kwargs.get("timeout")
            timeout = min(requested, remaining) if isinstance(requested, int | float) else remaining
            self._watcher.start_request(self._monotonic() + timeout)
            kwargs["timeout"] = SocketTimeout(total=timeout)
            with super().send(request, **kwargs) as response:
                if response.status_code != 200:
                    self.error_type = str(response.status_code)
                else:
                    self._read_acknowledgement(response)
                if self.batch_expired():
                    raise Timeout("OTLP log export deadline expired")
                return response
        except RequestException as exc:
            self.error_type = type(exc).__name__
            if self.batch_expired() and not isinstance(exc, Timeout):
                self.error_type = "Timeout"
                raise Timeout("OTLP log export deadline expired") from exc
            raise
        finally:
            self._watcher.finish_request()

    def _read_acknowledgement(self, response: Response) -> None:
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if self.batch_expired():
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
        deadline: LogExportDeadline | None = None,
    ) -> None:
        deadline = deadline or LogExportDeadline(monotonic)
        self._watcher = LogExportSocketWatcher(deadline)
        self._session = _LogExportSession(
            monotonic=monotonic, timeout=timeout, deadline=deadline, watcher=self._watcher
        )
        self._exporter = OTLPLogExporter(
            endpoint=endpoint,
            timeout=timeout,
            session=self._session,
            meter_provider=NoOpMeterProvider(),
        )
        self._exported = create_otel_sdk_exporter_log_exported(meter)
        self._flush_queue_losses = flush_queue_losses
        self._max_request_bytes = max_request_bytes
        self._watcher.start()

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self._session.start_batch()
        try:
            return self._export_bounded(batch)
        finally:
            self._flush_queue_losses()

    def _export_bounded(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        if self._session.batch_expired():
            return self._count(len(batch), "Timeout")
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
        try:
            self._exporter.shutdown()
        finally:
            self._watcher.stop()
            self._flush_queue_losses()
