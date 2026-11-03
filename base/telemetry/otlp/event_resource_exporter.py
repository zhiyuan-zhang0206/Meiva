from __future__ import annotations

from collections.abc import Sequence

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.resources import Resource


# meiva:note
# One event attribute as a resource dimension: Resource labels are scalars, and the three the
# exporter copies (`event_name`, `agent_id`, `cluster`) always are.
def _dimension(value: object) -> str | bool | int | float:
    if not isinstance(value, str | bool | int | float):
        raise TypeError(f"an event resource dimension must be a scalar, not {type(value).__name__}")
    return value


# meiva:note
# Give each event its own OTLP resource dimensions before serialization.
#
# A ``LoggerProvider`` has one static Resource, but Loki indexes resource attributes and the OTLP
# encoder groups a batch by that Resource. Rewriting a shared resource downstream therefore makes
# the last record's dimensions describe every record in the batch. This wrapper keeps one bounded
# SDK exporter worker while handing the encoder a resource that matches each individual record; the
# encoder then emits resource-homogeneous groups.
#
# This module imports the OTel SDK at module level and is only imported while the providers are
# built, so flag-off processes never pay for the SDK (see `telemetry_otlp_metrics.build_providers`).
class EventDimensionResourceExporter(LogRecordExporter):
    def __init__(self, exporter: LogRecordExporter) -> None:
        self._exporter = exporter

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        resource_tagged: list[ReadableLogRecord] = []
        for record in batch:
            dimensions = dict(record.resource.attributes)
            attributes = record.log_record.attributes or {}
            dimensions["event_name"] = _dimension(attributes["event_name"])
            if "agent_id" in attributes:
                dimensions["agent_id"] = _dimension(attributes["agent_id"])
            if "cluster" in attributes:
                dimensions["cluster"] = _dimension(attributes["cluster"])
            resource_tagged.append(
                ReadableLogRecord(
                    log_record=record.log_record,
                    resource=Resource(dimensions, schema_url=record.resource.schema_url),
                    instrumentation_scope=record.instrumentation_scope,
                    limits=record.limits,
                )
            )
        return self._exporter.export(resource_tagged)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 10_000) -> bool:
        return self._exporter.force_flush(timeout_millis)
