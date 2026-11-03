# meiva:note
# OTel SDK construction for the OTLP backend: providers, metric views and the metrics Resource.
# This module imports the SDK at module level on purpose: it is imported only inside the owned
# worker's bring-up, so flag-off processes and cold exec children never pay for the SDK.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from base.telemetry.delivery.loss import install_exporter_drop_observer
from base.telemetry.otlp.event_resource_exporter import EventDimensionResourceExporter
from base.telemetry.otlp.telemetry_otlp_metrics import (
    EVENT_LOOP_LAG_BUCKETS_MS,
    LLM_LATENCY_BUCKETS_MS,
    OTLP_HTTP_TIMEOUT_S,
    metrics_resource_attributes,
    provider_build_plan,
)

# meiva:note
# Metrics export cadence (PeriodicExportingMetricReader). 15 s keeps Grafana near-live without a
# per-batch network round-trip; the reader thread owns the timing, the emitter never waits on it.
_METRICS_INTERVAL_S = 15


# meiva:note
# Shape long LLM latencies and recovered gateway event-loop stalls.
# LLM percentiles are read per model/fleet, never per agent, so dropping `agent_id` removes the
# per-(agent, model) histogram fan-out. Event-loop lag retains minute-scale stalls that the OTel
# 10s default clips. Views match the INSTRUMENT name (unit suffix already stripped).
def metric_views() -> list[View]:
    llm_aggregation = ExplicitBucketHistogramAggregation(list(LLM_LATENCY_BUCKETS_MS))
    views = [
        View(
            instrument_name=name,
            aggregation=llm_aggregation,
            attribute_keys={"machine", "process", "model"},
        )
        for name in ("meiva_llm_usage_latency", "meiva_llm_usage_decode")
    ]
    views.append(
        View(
            instrument_name="meiva_gateway_event_loop_lag",
            aggregation=ExplicitBucketHistogramAggregation(list(EVENT_LOOP_LAG_BUCKETS_MS)),
            attribute_keys={"machine", "process"},
        )
    )
    return views


# meiva:note
# The metrics-side OTel Resource: `service.name=meiva-<process>` (the bounded process dimension)
# plus a per-process-instance `service.instance.id`. Without it every series lands as
# job="unknown_service" and two same-named processes exporting the same counter collide into ONE
# series with interleaved cumulative values (increase() reads garbage). Metrics only: the logs
# Resource is deliberately untouched, because Loki default-promotes resource attributes to index
# labels, so a per-process-unique instance id there would mint a new stream per process start.
# Per-agent event indexing (Task #1327) keeps this resource as-is: the collector promotes the
# per-record agent_id/event_name resource attributes (set in EventDimensionResourceExporter) to
# Loki index labels, and the read side era-slices its selector around the cutover
# (base/telemetry/loki_index_labels.py), so no service_name rewrite is needed.
def metrics_resource(*, home: Path) -> Resource:
    return Resource.create(metrics_resource_attributes(home=home))


# meiva:note
# Build the production (LoggerProvider, MeterProvider) pair exporting OTLP/HTTP to ``endpoint``
# (signal paths /v1/logs, /v1/metrics are appended by the exporters).
def build_providers(
    endpoint: str,
    *,
    keep_logs: Callable[[LoggerProvider], None] | None = None,
    keep_reader: Callable[[PeriodicExportingMetricReader], None] | None = None,
    keep_metrics: Callable[[MeterProvider], None] | None = None,
    mirror_dir: Path,
    home: Path,
) -> tuple[LoggerProvider, MeterProvider]:
    install_exporter_drop_observer(mirror_dir=mirror_dir)
    plan = provider_build_plan(
        endpoint,
        http_timeout_s=OTLP_HTTP_TIMEOUT_S,
        metrics_interval_s=_METRICS_INTERVAL_S,
    )
    # meiva:note
    # `shutdown_on_exit=False` on both providers: the SDK default registers a
    # provider shutdown on atexit at bring-up (mid-life), which sits AFTER
    # `base.telemetry.drain_on_exit` (registered at import) in the LIFO exit
    # order and therefore fires FIRST. The emitter's exit flush then lands on a
    # shut-down processor (`force_flush` returns False) and a record emitted
    # inside the drain thread's final batch window — every short-lived
    # process's tail record — stays mirror-only (task #4314 triage / #4320).
    # `_drain_on_exit` is the single ordered exit seam: it flushes the emitter
    # and this queue, then force-flushes these still-live providers.
    logs = LoggerProvider(shutdown_on_exit=plan.shutdown_on_exit)
    if keep_logs is not None:
        keep_logs(logs)
    log_exporter = EventDimensionResourceExporter(
        OTLPLogExporter(endpoint=plan.logs_endpoint, timeout=plan.http_timeout_s)
    )
    logs.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter, export_timeout_millis=plan.export_timeout_millis)
    )
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=plan.metrics_endpoint, timeout=plan.http_timeout_s),
        export_interval_millis=plan.metrics_interval_millis,
        export_timeout_millis=plan.export_timeout_millis,
    )
    if keep_reader is not None:
        keep_reader(reader)
    metrics = MeterProvider(
        shutdown_on_exit=plan.shutdown_on_exit,
        metric_readers=[reader],
        resource=metrics_resource(home=home),
        views=metric_views(),
    )
    if keep_metrics is not None:
        keep_metrics(metrics)
    return logs, metrics
