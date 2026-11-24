from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from opentelemetry.metrics import Observation
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from base import telemetry
from base.clock.tests.fakes import system_clock
from base.config import settings
from base.db import Database
from base.paths import meiva_home
from base.telemetry.delivery.defer_plan import CHILD_DEFER_MAX_AGE_DEFAULT_S, ChildDeferPlan
from base.telemetry.delivery.loss import report_loss
from base.telemetry.delivery.receipts import Category, Event, Level
from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_sdk
from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from base.telemetry.tests.otlp.identity_inputs import machine_identity

OtlpFixture = tuple[telemetry_otlp.OtlpBackend, InMemoryLogRecordExporter, InMemoryMetricReader]

AGENT = 8902
CHILD_DEFER = ChildDeferPlan(enabled=True, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S)


def otlp_test_event(
    *,
    event_name: str = "llm_usage",
    category: Category = "telemetry",
    level: Level = "info",
    attributes: dict[str, Any] | None = None,
    agent_id: int | None = AGENT,
    trace_id: str | None = "abcd" * 8,
    span_id: str | None = "ef01" * 4,
) -> Event:
    return Event(
        ts=datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC),
        trace_id=trace_id,
        span_id=span_id,
        agent_id=agent_id,
        machine="test-mac",
        cluster=".meiva-test",
        process="test-proc",
        category=category,
        event_name=event_name,
        level=level,
        source="test",
        target_agent_id=None,
        attributes=dict(attributes or {}),
    )


@pytest.fixture
def otlp_backend(
    tmp_path: Path,
) -> Iterator[OtlpFixture]:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider

    from base.telemetry.otlp.event_resource_exporter import EventDimensionResourceExporter

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    resource_exporter = EventDimensionResourceExporter(log_exporter)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(resource_exporter))
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])

    backend = telemetry_otlp.OtlpBackend(
        now=system_clock().now,
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logger_provider, metric_provider),
        mirror_dir=tmp_path,
        home=tmp_path,
        endpoint=lambda: "http://127.0.0.1:4318",
    )

    pipeline = telemetry.process_state["pipeline"]
    assert isinstance(pipeline, ExportingEventPipeline)
    pipeline._exporter = backend
    yield backend, log_exporter, metric_reader
    backend.shutdown()


def collected_metrics(metric_reader: Any) -> dict[str, Any]:
    data = metric_reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


def _assert_log_record_severity_and_correlation(r: Any) -> None:
    assert r.log_record.severity_text == "warning"
    assert r.log_record.severity_number.value == 13
    assert r.log_record.trace_id == int("abcd" * 8, 16)
    assert r.log_record.span_id == int("ef01" * 4, 16)


def _assert_log_record_indexed_attributes(r: Any) -> None:
    attrs = dict(r.log_record.attributes)
    assert attrs["event_name"] == "exec"
    assert attrs["category"] == "log"
    assert attrs["level"] == "warning"
    assert attrs["machine"] == "test-mac"
    assert attrs["cluster"] == ".meiva-test"
    assert attrs["process"] == "test-proc"
    assert attrs["source"] == "test"
    assert attrs["agent_id"] == AGENT


def _assert_log_record_json_body(r: Any) -> None:
    body = json.loads(r.log_record.body)
    assert body["event_name"] == "exec"
    assert body["cluster"] == ".meiva-test"
    assert body["attributes"] == {"body": "print(1)", "ok": False}
    assert body["ts"] == "2026-08-11T12:00:00+00:00"


def test_log_mapping_full_record_shape(otlp_backend: OtlpFixture) -> None:
    backend, log_exporter, _ = otlp_backend
    event = otlp_test_event(
        event_name="exec",
        category="log",
        level="warning",
        attributes={"body": "print(1)", "ok": False},
    )
    backend.export_batch([event])
    backend.flush()

    records = log_exporter.get_finished_logs()
    assert len(records) == 1
    r = records[0]
    _assert_log_record_severity_and_correlation(r)
    _assert_log_record_indexed_attributes(r)
    _assert_canonical_log_body(r, event)
    _assert_log_record_json_body(r)


def _assert_canonical_log_body(record: Any, event: Event) -> None:
    assert record.log_record.body == telemetry.event_line(event)


def test_flush_groups_each_event_name_under_its_matching_resource(
    otlp_backend: OtlpFixture,
) -> None:
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs

    backend, log_exporter, _ = otlp_backend
    backend.export_batch(
        [otlp_test_event(event_name=name) for name in ("llm_usage", "node_exit", "log")]
    )
    backend.flush()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        records = log_exporter.get_finished_logs()
        if len(records) == 3:
            break
        time.sleep(0.01)
    records = log_exporter.get_finished_logs()
    request = encode_logs(records)
    assert len(request.resource_logs) == 3
    for resource_logs in request.resource_logs:
        resource_event_name = next(
            attribute.value.string_value
            for attribute in resource_logs.resource.attributes
            if attribute.key == "event_name"
        )
        resource_cluster = next(
            attribute.value.string_value
            for attribute in resource_logs.resource.attributes
            if attribute.key == "cluster"
        )
        assert resource_cluster == ".meiva-test"
        for scope_logs in resource_logs.scope_logs:
            for record in scope_logs.log_records:
                assert (
                    next(
                        attribute.value.string_value
                        for attribute in record.attributes
                        if attribute.key == "event_name"
                    )
                    == resource_event_name
                )


def test_only_explicit_metrics_export_with_fixed_kinds(otlp_backend: OtlpFixture) -> None:
    backend, log_exporter, metric_reader = otlp_backend
    backend.export_batch(
        [
            otlp_test_event(
                event_name="llm_usage",
                attributes={
                    "model": "custom-model",
                    "latency_ms": 42,
                    "calls": 1,
                    "in_total": 100,
                    "out_total": 50,
                    "cost_usd": 0.125,
                    "unpriced": 0,
                    "price_miss": 3.0,
                    "cache_read": 90,
                    "reasoning": 10,
                },
            ),
            otlp_test_event(
                event_name="turn_end",
                attributes={"ok": True, "duration_seconds": 4, "thread_id": "uuid"},
            ),
            otlp_test_event(
                event_name="gateway_latency",
                attributes={"count": 3, "p95_ms": 42.5, "route": "/agents/42"},
            ),
            otlp_test_event(
                event_name="delta_message_suffix",
                attributes={"fetched_rows": 5, "checkpoint_id": "uuid"},
            ),
        ]
    )
    backend.flush()

    metrics = collected_metrics(metric_reader)
    assert set(metrics) == {
        "meiva_llm_usage_latency",
        "meiva_turn_end_duration",
        "meiva_gateway_latency_count",
    }
    llm = metrics["meiva_llm_usage_latency"]
    assert llm.unit == "ms"
    assert llm.data.data_points[0].sum == 42
    turn = metrics["meiva_turn_end_duration"]
    assert turn.unit == "s"
    assert turn.data.data_points[0].sum == 4
    gateway = metrics["meiva_gateway_latency_count"]
    assert gateway.data.is_monotonic
    assert gateway.data.data_points[0].value == 3
    assert all(not metric.data.data_points[0].attributes for metric in metrics.values())
    logs = log_exporter.get_finished_logs()
    assert len(logs) == 4
    usage = logs[0].log_record
    assert isinstance(usage.body, str)
    assert json.loads(usage.body)["attributes"]["cost_usd"] == 0.125


def test_usage_ledger_failure_keeps_the_loki_event_without_a_metric(
    otlp_backend: OtlpFixture,
) -> None:
    backend, log_exporter, metric_reader = otlp_backend
    event = otlp_test_event(
        event_name="llm_usage_ledger_write_failed",
        level="warning",
        attributes={
            "call_id": "api:receipt",
            "reason": "database",
            "error_type": "QueryCanceled",
            "failures": 1,
        },
    )
    backend.export_batch([event])
    backend.flush()

    [record] = log_exporter.get_finished_logs()
    assert (record.log_record.attributes or {})["event_name"] == event.event_name
    _assert_canonical_log_body(record, event)
    assert collected_metrics(metric_reader) == {}


@pytest.mark.parametrize("process", ["agent-exec", "sdk", "schedule-runner", "cli-health-probe"])
def test_short_lived_process_reports_queue_loss_on_the_metrics_lane(
    otlp_backend: OtlpFixture, process: str
) -> None:
    backend, log_exporter, metric_reader = otlp_backend
    event = otlp_test_event(attributes={"latency_ms": 250.0})
    backend.export_batch([replace(event, process=process)])
    backend.flush()

    assert collected_metrics(metric_reader) == {}
    assert len(log_exporter.get_finished_logs()) == 1

    now = system_clock().now()
    report_loss(
        replace(event, process=process), 3, "emitter", now=now, record_metric=backend.record_loss
    )
    backend.flush()

    metrics = collected_metrics(metric_reader)
    assert set(metrics) == {"meiva_event_log_drop_last_dropped_at"}
    point = metrics["meiva_event_log_drop_last_dropped_at"].data.data_points[0]
    assert point.value == now.timestamp()
    assert dict(point.attributes) == {"process": process, "queue": "emitter"}
    assert len(log_exporter.get_finished_logs()) == 1


def test_short_lived_metric_view_exports_only_queue_loss() -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], views=telemetry_otlp_sdk.metric_views("sdk"))
    try:
        meter = provider.get_meter("meiva.telemetry")
        meter.create_counter("otel.sdk.exporter.log.exported").add(2)
        meter.create_histogram("meiva_llm_usage_latency").record(250.0)
        assert reader.get_metrics_data() is None
        meter.create_observable_gauge(
            "meiva_event_log_drop_last_dropped_at",
            callbacks=[lambda _: [Observation(123)]],
        )
        assert set(collected_metrics(reader)) == {"meiva_event_log_drop_last_dropped_at"}
    finally:
        provider.shutdown()


def test_metrics_resource_carries_cluster(tmp_path: Path) -> None:
    home = tmp_path / ".meiva-preview"
    home.mkdir()

    resource = telemetry_otlp_sdk.metrics_resource(home=home, instance_id="test-instance")

    assert resource.attributes["cluster"] == ".meiva-preview"


def test_root_health_tick_uses_latest_timestamp_gauge(otlp_backend: OtlpFixture) -> None:
    backend, _, metric_reader = otlp_backend
    backend.export_batch(
        [
            otlp_test_event(
                event_name="root_health_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_000.0},
            ),
            otlp_test_event(
                event_name="root_health_tick",
                attributes={"last_tick_timestamp_seconds": 1_725_000_060.0},
            ),
        ]
    )
    backend.flush()

    tick = collected_metrics(metric_reader)["meiva_root_health_tick_last_tick_timestamp"]
    assert tick.unit == "s"
    assert tick.data.data_points[0].value == 1_725_000_060.0


def test_root_health_gauges_keep_separate_home_identity_and_expected_only(
    otlp_backend: OtlpFixture,
) -> None:
    backend, _, metric_reader = otlp_backend
    backend.export_batch(
        [
            otlp_test_event(
                event_name="root_health_expected",
                attributes={
                    "home_id": "a" * 64,
                    "expected_since_timestamp_seconds": 100.0,
                },
            ),
            otlp_test_event(
                event_name="root_health_expected",
                attributes={
                    "home_id": "b" * 64,
                    "expected_since_timestamp_seconds": 120.0,
                },
            ),
            otlp_test_event(
                event_name="root_health_tick",
                attributes={
                    "home_id": "a" * 64,
                    "last_tick_timestamp_seconds": 150.0,
                },
            ),
        ]
    )
    backend.flush()
    metrics = collected_metrics(metric_reader)
    expected = metrics["meiva_root_health_expected_expected_since_timestamp"]
    assert {point.attributes["home_id"] for point in expected.data.data_points} == {
        "a" * 64,
        "b" * 64,
    }
    ticks = metrics["meiva_root_health_tick_last_tick_timestamp"]
    assert {point.attributes["home_id"] for point in ticks.data.data_points} == {"a" * 64}


def test_metric_view_keeps_the_llm_latency_tail() -> None:
    from opentelemetry.sdk.metrics import MeterProvider

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        metric_readers=[reader], views=telemetry_otlp_sdk.metric_views("agent_host")
    )
    meter = provider.get_meter("meiva.telemetry")
    hist = meter.create_histogram("meiva_llm_usage_latency", unit="ms")
    hist.record(45000.0)

    dp = collected_metrics(reader)["meiva_llm_usage_latency"].data.data_points[0]
    assert list(dp.explicit_bounds) == list(telemetry_otlp.LLM_LATENCY_BUCKETS_MS)
    assert dp.bucket_counts[list(dp.explicit_bounds).index(60000)] == 1


def test_non_telemetry_events_produce_no_metrics(otlp_backend: OtlpFixture) -> None:
    backend, log_exporter, metric_reader = otlp_backend
    backend.export_batch(
        [
            otlp_test_event(event_name="exec", category="log", attributes={"n": 3, "ok": True}),
            otlp_test_event(event_name="status_change", category="audit", attributes={"code": 0}),
        ]
    )
    backend.flush()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if len(log_exporter.get_finished_logs()) == 2:
            break
        time.sleep(0.01)
    assert len(log_exporter.get_finished_logs()) == 2
    assert collected_metrics(metric_reader) == {}


def test_warmup_initializes_enabled_backend(
    otlp_backend: tuple[telemetry_otlp.OtlpBackend, Any, Any],
) -> None:
    backend, _log_exporter, _metric_reader = otlp_backend

    backend.warmup()

    assert backend._logs is not None
    assert backend._metric_provider is not None
    assert backend._thread is not None


def test_the_agent_host_reports_the_swap_pool_as_the_semconv_paging_usage_metric(
    otlp_backend: OtlpFixture,
) -> None:
    backend, _log_exporter, metric_reader = otlp_backend
    pipeline = telemetry.process_state["pipeline"]
    assert isinstance(pipeline, ExportingEventPipeline)
    telemetry.init_telemetry(home=meiva_home(), process="agent_host", pipeline=pipeline)

    backend.warmup()

    usage = collected_metrics(metric_reader)["system.paging.usage"]
    assert usage.unit == "By"
    assert sorted(point.attributes["state"] for point in usage.data.data_points) == ["free", "used"]


def test_a_process_that_is_not_the_agent_host_reports_no_swap_pool(
    otlp_backend: OtlpFixture,
) -> None:
    backend, _log_exporter, metric_reader = otlp_backend

    backend.warmup()

    assert "system.paging.usage" not in collected_metrics(metric_reader)


def test_export_batch_builds_backend_once(
    otlp_backend: tuple[telemetry_otlp.OtlpBackend, Any, Any],
) -> None:
    backend, _log_exporter, _metric_reader = otlp_backend

    backend.export_batch([otlp_test_event()])
    first = backend._logs
    assert first is not None
    backend.export_batch([otlp_test_event()])
    assert backend._logs is first


def test_flush_without_init_is_noop(tmp_path: Path) -> None:
    backend = telemetry_otlp.OtlpBackend(
        now=system_clock().now,
        export_allowed=lambda: True,
        mirror_dir=tmp_path,
        home=tmp_path,
        enabled=lambda: True,
        endpoint=lambda: "http://127.0.0.1:4318",
        instance_id="test-instance",
    )
    backend.flush()
    assert backend._thread is None
    assert backend._queue.empty()


def test_flag_off_disables_export(emitted_events: EmittedEvents, tmp_path: Path) -> None:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])
    backend = telemetry_otlp.OtlpBackend(
        now=system_clock().now,
        export_allowed=lambda: True,
        enabled=lambda: False,
        providers=(logger_provider, metric_provider),
        mirror_dir=tmp_path,
        home=tmp_path,
        endpoint=lambda: "http://127.0.0.1:4318",
    )

    backend.export_batch([otlp_test_event()])
    assert len(emitted_events) == 0, "flag-off must not emit backend status events"
    assert backend._logs is None
    assert backend._queue.empty()
    assert log_exporter.get_finished_logs() == ()
    assert metric_reader.get_metrics_data() is None


def test_flag_defaults_on_with_standard_endpoint() -> None:
    fields = type(settings.observability).model_fields
    assert fields["telemetry_otlp_enabled"].default is True
    assert fields["telemetry_otlp_endpoint"].default == "http://127.0.0.1:4318"


@pytest.fixture(autouse=True)
def bind_telemetry(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path
) -> Iterator[None]:
    pipeline = telemetry.build_pipeline(
        now=system_clock().now,
        database=lambda: database,
        machine=machine_identity(),
        home=tmp_path,
        otlp_enabled=lambda: False,
        otlp_endpoint=lambda: "http://127.0.0.1:4318",
        rng=random.SystemRandom(),
    )
    try:
        telemetry.init_telemetry(home=meiva_home(), process="test-proc", pipeline=pipeline)
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO agents (id) VALUES (%s) ON CONFLICT DO NOTHING", (AGENT,))
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'running') "
                "ON CONFLICT DO NOTHING",
                (AGENT,),
            )
        db_conn.commit()
        yield
    finally:
        pipeline.stop(timeout=2)


# meiva:note
# The providers are built for the home and mirror directory the exporter was constructed with. Both
# are directories under `tmp_path`, so neither equals the process's own home: an exporter that read
# the environment, or dropped one of the two, fails here. The endpoint is a non-HTTP URL, which the
# reachability probe accepts without dialing, so the real SDK providers are built and shut down. The
# mirror shows up by behaviour: the SDK's queue-overflow warning is recorded under it.
def test_the_providers_are_built_for_the_exporters_own_home_and_mirror(tmp_path: Path) -> None:
    import logging
    from types import SimpleNamespace

    from base.telemetry.observability import cluster_label

    home, mirror = tmp_path / "exporter-home", tmp_path / "exporter-mirror"
    home.mkdir()
    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    old_filters = list(sdk_logger.filters)
    backend = telemetry_otlp.OtlpBackend(
        now=system_clock().now,
        export_allowed=lambda: True,
        endpoint=lambda: "file:///collector",
        mirror_dir=mirror,
        home=home,
        enabled=lambda: True,
        instance_id="test-instance",
    )
    worker = SimpleNamespace(logs=None, metrics=None, metric_reader=None)

    assert backend._initialize_worker(worker)  # pyright: ignore[reportArgumentType]
    try:
        assert cluster_label(home) != cluster_label(Path("/"))
        assert worker.metrics._sdk_config.resource.attributes["cluster"] == cluster_label(home)
        sdk_logger.warning("Queue full, dropping %s.", "Log")
        worker.logs.shutdown()
        day = datetime.now(UTC).strftime("%Y%m%d")
        rows = [
            json.loads(line)
            for line in (mirror / f"events-{day}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [row["event_name"] for row in rows] == ["event_log_drop"]
    finally:
        sdk_logger.filters[:] = old_filters
        worker.metrics.shutdown()
