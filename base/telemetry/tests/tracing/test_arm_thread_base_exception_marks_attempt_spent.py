"""Tracing cases: arm thread base exception marks attempt spent."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from base.clock.tests.fakes import system_clock
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry import tracing as tracing_mod
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.tests.capture.trace_process import run_trace_scenario
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tests.test_tracing import (
    CollectorStubServer,
    stub_traceloop_init,
    trace_config,
    wait_init_resolved,
)
from base.telemetry.tests.test_tracing import (
    collector_up as collector_up,
)
from base.telemetry.tests.test_tracing import (
    reset_trace_workers as reset_trace_workers,
)
from base.telemetry.tests.test_tracing import trace_readiness as trace_readiness
from base.telemetry.tracing import (
    OtlpJsonHttpSpanExporter,
    initialize_tracing,
)


def test_arm_thread_base_exception_marks_attempt_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    # meiva:note
    # The one-attempt contract holds for ANY arm-thread death: a
    # BaseException escape (SystemExit/GeneratorExit from inside the SDK — not
    # expected, but not impossible) must still resolve readiness as false, so a later
    # initialize_tracing(machine=machine_identity(), home=Path(".meiva")) cannot re-spawn the arm thread past the dead
    # is_alive() guard (the QA #1060 corner: with `except Exception` only, a
    # dead arm thread carrying no flag left one-attempt-per-process bypassable).
    #
    home = tmp_path / ".meiva"
    home.mkdir()
    calls: list[int] = []

    def _exit_sdk(**_kw: object) -> None:
        calls.append(1)
        raise SystemExit("sdk guard exit")

    stub_traceloop_init(monkeypatch, _exit_sdk)

    initialize_tracing(
        now=system_clock().now,
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert trace_readiness.resolved and not trace_readiness.ready
    assert trace_readiness.ready is False

    initialize_tracing(
        now=system_clock().now,
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )  # must be a no-op: the attempt was spent
    assert calls == [1]


def test_timeout_then_late_arm_recording_comes_up_midlife(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "late-success", lifecycle=True)


def test_readiness_returns_without_waiting_when_no_arm_was_admitted(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "no-admission", lifecycle=True)


def test_otlp_exporter_posts_protobuf(collector_up: CollectorStubServer) -> None:
    """Each export() batch becomes one OTLP ExportTraceServiceRequest POSTed
    to <endpoint>/v1/traces with Content-Type application/x-protobuf (the wire
    format the collector's OTLP receiver accepts — its JSON receiver rejects
    the SDK's padded-base64 ids); the body parses back to the same spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=True)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("meiva.session")
    with tracer.start_as_current_span("meiva-agent-7") as root:
        root.set_attribute("session.id", "7")
        with tracer.start_as_current_span("child"):
            pass
    provider.shutdown()

    posts = collector_up.posts
    assert len(posts) >= 1
    url, _body, headers = posts[0]
    assert url == f"{collector_up.endpoint}/v1/traces"
    assert headers["Content-Type"] == "application/x-protobuf"

    # The body is the OTLP ExportTraceServiceRequest protobuf; it must parse
    # back to exactly the recorded spans (what Tempo ingests).
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    spans = 0
    for _, raw, _h in posts:
        req = ExportTraceServiceRequest()
        req.ParseFromString(raw)
        assert req.SerializeToString()
        spans += sum(len(ss.spans) for rs in req.resource_spans for ss in rs.scope_spans)
    assert spans == 2


def test_otlp_exporter_timeout_is_bounded_and_returns_failure(
    collector_up: CollectorStubServer,
) -> None:
    """A collector timeout costs one bounded POST and never escapes the exporter."""
    block = threading.Event()
    collector_up.block_event = block
    exporter = OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=True, timeout_s=0.05)
    try:
        assert exporter.export([]) is tracing_mod.SpanExportResult.FAILURE
        assert 0 < tracing_mod.TRACE_EXPORT_TIMEOUT_S <= 5.0
    finally:
        block.set()
        collector_up.block_event = None


def test_otlp_exporter_circuit_drops_during_cooldown_then_recovers(
    collector_up: CollectorStubServer,
) -> None:
    """Consecutive failures open the circuit; one post-cooldown probe closes it."""
    collector_up.status_sequence = [503] * tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD
    exporter = OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=True)

    now = 100.0
    for _ in range(tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD):
        assert exporter.export([], now=now) is tracing_mod.SpanExportResult.FAILURE
    assert len(collector_up.posts) == tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD

    assert exporter.export([], now=now) is tracing_mod.SpanExportResult.FAILURE
    assert len(collector_up.posts) == tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD
    assert exporter._dropped_batches == 1

    now += tracing_mod.TRACE_EXPORT_COOLDOWN_S
    assert exporter.export([], now=now) is tracing_mod.SpanExportResult.SUCCESS
    assert len(collector_up.posts) == tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD + 1
    assert exporter._consecutive_failures == 0

    assert exporter.export([], now=now) is tracing_mod.SpanExportResult.SUCCESS
    assert len(collector_up.posts) == tracing_mod.TRACE_EXPORT_FAILURE_THRESHOLD + 2


def test_turn_span_exports_isolated_root_with_causal_links_at_exit(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "root")


def test_turn_span_noop_when_disabled(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "turn-disabled")


def test_turn_span_noop_when_initialize_skipped(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "turn-uninitialized")


def test_turn_span_opens_root_with_session_id(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "root")


def test_claim_idle_wait_span_noop_when_disabled(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "idle-disabled")


def test_claim_idle_wait_span_noop_when_initialize_skipped(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "idle-uninitialized")


def test_claim_idle_wait_span_ends_node_span_and_opens_idle_wait(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "node")


def test_claim_idle_wait_span_never_ends_non_node_span(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "nonnode")


def test_claim_idle_wait_span_skips_non_recording_span(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "nonrecording")
