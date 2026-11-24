# meiva:note
# OTel SDK construction for the OTLP backend: providers, metric views and the metrics Resource.
# This module imports the SDK at module level on purpose: it is imported only inside the owned
# worker's bring-up, so flag-off processes and cold exec children never pay for the SDK.

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.environment_variables import OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import DropAggregation, ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from base.telemetry.delivery.loss import install_exporter_drop_observer
from base.telemetry.delivery.state import process_name
from base.telemetry.observability import cluster_identity_attributes
from base.telemetry.otlp.event_resource_exporter import EventDimensionResourceExporter
from base.telemetry.otlp.http_logs.deadline import LogExportDeadline
from base.telemetry.otlp.http_logs.exporter import BoundedHTTPLogExporter
from base.telemetry.otlp.plans import LOSS_ONLY_PROCESSES
from base.telemetry.otlp.telemetry_otlp_metrics import (
    LLM_LATENCY_BUCKETS_MS,
    metrics_resource_attributes,
    provider_build_plan,
)

# meiva:note
# Metrics export cadence (PeriodicExportingMetricReader). 15 s keeps Grafana near-live without a
# per-batch network round-trip; the reader thread owns the timing, the emitter never waits on it.
_METRICS_INTERVAL_S = 15
_METRICS_HTTP_TIMEOUT_S = 2.0
_LOGS_HTTP_TIMEOUT_S = 3.5


# meiva:note
# Keep the queue-loss gauge independent of the log queue. Short-lived roles suppress all
# other instruments, including the SDK's own export metrics, so only actual loss creates series.
def metric_views(process: str) -> list[View]:
    if process in LOSS_ONLY_PROCESSES:
        return [
            View(instrument_name="*", aggregation=DropAggregation()),
            View(instrument_name="meiva_event_log_drop_last_dropped_at"),
        ]
    return [
        View(
            instrument_name="meiva_llm_usage_latency",
            aggregation=ExplicitBucketHistogramAggregation(list(LLM_LATENCY_BUCKETS_MS)),
        )
    ]


# meiva:note
# The metrics-side OTel Resource: `service.name=meiva-<process>` (the bounded process dimension)
# plus a per-process-instance `service.instance.id`. Without it every series lands as
# job="unknown_service" and two same-named processes exporting the same counter collide into ONE
# series with interleaved cumulative values (increase() reads garbage). Metrics only: the logs
# Resource gets no instance id, because Loki default-promotes resource attributes to index
# labels, so a per-process-unique instance id there would mint a new stream per process start
# (the logs Resource carries just the per-cluster routing identity, below).
# Per-agent event indexing (Task #1327) keeps this resource as-is: the collector promotes the
# per-record agent_id/event_name resource attributes (set in EventDimensionResourceExporter) to
# Loki index labels, and the read side era-slices its selector around the cutover
# (base/telemetry/loki_index_labels.py), so no service_name rewrite is needed.
def metrics_resource(*, home: Path, instance_id: str) -> Resource:
    return Resource.create(metrics_resource_attributes(home=home, instance_id=instance_id))


# meiva:note
# Build the production (LoggerProvider, MeterProvider) pair exporting OTLP/HTTP to ``endpoint``
# (signal paths /v1/logs, /v1/metrics are appended by the exporters).
def build_providers(
    endpoint: str,
    *,
    keep_logs: Callable[[LoggerProvider], None] | None = None,
    keep_reader: Callable[[PeriodicExportingMetricReader], None] | None = None,
    keep_metrics: Callable[[MeterProvider], None] | None = None,
    home: Path,
    instance_id: str,
    log_deadline: LogExportDeadline | None = None,
) -> tuple[LoggerProvider, MeterProvider]:
    queue_losses = install_exporter_drop_observer()
    os.environ.setdefault(OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED, "true")
    plan = provider_build_plan(
        endpoint,
        http_timeout_s=_METRICS_HTTP_TIMEOUT_S,
        metrics_interval_s=_METRICS_INTERVAL_S,
    )
    # meiva:note
    # `shutdown_on_exit=False` on both providers: the SDK default registers a
    # provider shutdown on atexit at bring-up (mid-life), which sits AFTER
    # `base.telemetry.drain_on_exit` (registered at import) in the LIFO exit
    # order and therefore fires FIRST. The emitter's exit flush then lands on a
    # shut-down processor (`force_flush` returns False) and a record emitted
    # inside the drain thread's final batch window — every short-lived
    # process's tail record — is never exported (task #4314 triage / #4320).
    # `_drain_on_exit` is the single ordered exit seam: it flushes the emitter
    # and this queue, then force-flushes these still-live providers.
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
        resource=metrics_resource(home=home, instance_id=instance_id),
        views=metric_views(process_name()),
    )
    if keep_metrics is not None:
        keep_metrics(metrics)
    logs = LoggerProvider(
        resource=Resource.create(cluster_identity_attributes(home)),
        shutdown_on_exit=plan.shutdown_on_exit,
        meter_provider=metrics,
    )
    if keep_logs is not None:
        keep_logs(logs)
    log_exporter = EventDimensionResourceExporter(
        BoundedHTTPLogExporter(
            plan.logs_endpoint,
            timeout=_LOGS_HTTP_TIMEOUT_S,
            monotonic=time.monotonic,
            meter=metrics.get_meter("meiva.telemetry.otlp"),
            flush_queue_losses=queue_losses.flush,
            deadline=log_deadline,
        )
    )
    try:
        logs.add_log_record_processor(BatchLogRecordProcessor(log_exporter, meter_provider=metrics))
    except BaseException:
        log_exporter.shutdown()
        raise
    return logs, metrics
