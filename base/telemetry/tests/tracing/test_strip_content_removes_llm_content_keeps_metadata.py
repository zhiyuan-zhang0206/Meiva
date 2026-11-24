"""Tracing cases: strip content removes llm content keeps metadata."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest

from base.clock.tests.fakes import system_clock
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tests.test_tracing import (
    CollectorStubServer,
    otlp_with_attrs,
    stub_traceloop_init,
    trace_config,
    wait_init_resolved,
    warned_fields,
)
from base.telemetry.tests.test_tracing import collector_up as collector_up
from base.telemetry.tests.test_tracing import (
    reset_trace_workers as reset_trace_workers,
)
from base.telemetry.tests.test_tracing import trace_readiness as trace_readiness
from base.telemetry.tracing import OtlpJsonHttpSpanExporter, initialize_tracing


def test_strip_content_removes_llm_content_keeps_metadata():
    """strip_content_attributes removes gen_ai.task.input/output,
    traceloop.entity.input/output and messages-like keys; chain metadata and
    status survive."""
    from base.telemetry.otlp.trace_strip import strip_content_attributes

    otlp = otlp_with_attrs(
        {
            "gen_ai.task.input": '{"inputs": {"messages": ["you are meiva..."]}}',
            "gen_ai.task.output": "the full completion...",
            "traceloop.entity.input": '{"inputs": {...}}',
            "traceloop.entity.output": '{"outputs": {...}}',
            "messages": "[...]",
            "system_instructions": "[...]",
            "gen_ai.input.messages": "[...]",
            "gen_ai.output.messages": "[...]",
            "traceloop.association.properties.agent_id": "238",
            "traceloop.association.properties.langgraph_path": "after_init",
            "gen_ai.task.status": "success",
            "gen_ai.operation.name": "execute_task",
            "gen_ai.task.id": "019fd0ec-...",
            "session.id": "238",
        }
    )
    strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert keys == {
        "traceloop.association.properties.agent_id",
        "traceloop.association.properties.langgraph_path",
        "gen_ai.task.status",
        "gen_ai.operation.name",
        "gen_ai.task.id",
        "session.id",
    }


def test_strip_content_size_guard_drops_huge_strings():
    """The size guard drops any single attribute whose string payload exceeds
    MAX_ATTR_STRING_CHARS even when the key is not a known content key — a
    future instrumentor-invented content key cannot leak megabytes back."""
    from base.telemetry.otlp.trace_strip import MAX_ATTR_STRING_CHARS, strip_content_attributes

    big = "x" * (MAX_ATTR_STRING_CHARS + 1)
    otlp = otlp_with_attrs(
        {
            "traceloop.association.properties.agent_id": "238",
            "mystery.new.content.key": big,
        }
    )
    strip_content_attributes(otlp)  # pyright: ignore[reportUnknownArgumentType]
    keys = {kv["key"] for kv in otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert keys == {"traceloop.association.properties.agent_id"}


def test_strip_content_removes_event_attributes_too():
    """Content attributes nested under span events (the use_attributes=False
    path) are stripped as well."""
    from base.telemetry.otlp.trace_strip import strip_content_attributes

    otlp: dict[str, Any] = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "chat",
                                "attributes": [
                                    {
                                        "key": "gen_ai.task.status",
                                        "value": {"stringValue": "success"},
                                    }
                                ],
                                "events": [
                                    {
                                        "name": "gen_ai.input",
                                        "attributes": [
                                            {
                                                "key": "gen_ai.input.messages",
                                                "value": {"stringValue": "[...]"},
                                            },
                                            {
                                                "key": "gen_ai.usage.input_tokens",
                                                "value": {"intValue": "42"},
                                            },
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    strip_content_attributes(otlp)
    span = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ev_keys = {kv["key"] for kv in span["events"][0]["attributes"]}
    assert ev_keys == {"gen_ai.usage.input_tokens"}


def test_exporter_posts_stripped_line(collector_up: CollectorStubServer) -> None:
    """OtlpJsonHttpSpanExporter strips content attributes before the POST —
    stripped here, content never reaches the sidecar or Tempo
    (defensive layer 2)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=True))
    )
    tracer = provider.get_tracer("meiva.session")
    with tracer.start_as_current_span("meiva-agent-7") as root:
        root.set_attribute("session.id", "7")
        root.set_attribute("gen_ai.task.input", "secret prompt")
        root.set_attribute("traceloop.association.properties.agent_id", "7")
    provider.shutdown()
    posts = [body for _url, body, _headers in collector_up.posts]
    wire = b"".join(posts)
    assert b"secret prompt" not in wire  # content stripped before the wire
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    req = ExportTraceServiceRequest()
    req.ParseFromString(posts[0])
    keys = {
        kv["key"]
        for rs in MessageToDict(req)["resourceSpans"]
        for ss in rs["scopeSpans"]
        for sp in ss["spans"]
        for kv in sp.get("attributes", [])
    }
    assert "traceloop.association.properties.agent_id" in keys  # metadata survives


def test_exporter_keeps_http_metadata_when_llm_stripping_is_off(
    collector_up: CollectorStubServer,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import SpanKind

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=False))
    )
    tracer = provider.get_tracer("meiva.session")
    with tracer.start_as_current_span("GET /private/{id}", kind=SpanKind.SERVER) as span:
        span.set_attribute("http.route", "/private/{id}")
        span.set_attribute("http.target", "/private/1?token=secret-query")
        span.set_attribute("http.request.header.authorization", "secret-bearer")
        span.record_exception(ValueError("secret-exception"))
    with tracer.start_as_current_span("generate") as span:
        span.set_attribute("gen_ai.task.input", "benchmark prompt")
    provider.shutdown()

    wire = b"".join(body for _url, body, _headers in collector_up.posts)
    assert b"benchmark prompt" in wire
    assert b"http.route" in wire
    assert b"secret-query" not in wire
    assert b"secret-bearer" not in wire
    assert b"secret-exception" not in wire


# meiva:note
# The process root applies `TraceConfig.traceloop_environment()` before recording is armed: the
# SDK's one content switch is false whenever recording is on and strips content.
@pytest.mark.parametrize(
    ("enabled", "strip_content", "expected"),
    [
        (True, True, {"TRACELOOP_TRACE_CONTENT": "false"}),
        (True, False, {}),
        (False, True, {}),
        (False, False, {}),
    ],
)
def test_traceloop_environment_forces_content_off_only_when_recording_strips(
    *, enabled: bool, strip_content: bool, expected: dict[str, str]
) -> None:
    config = trace_config(enabled=enabled, strip_content=strip_content)
    assert dict(config.traceloop_environment()) == expected


def test_initialize_leaves_the_environment_to_its_process_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    calls: list[dict[str, Any]] = []

    def record_init(**kw: Any) -> None:
        calls.append(kw)

    stub_traceloop_init(monkeypatch, record_init)
    before = dict(os.environ)

    initialize_tracing(
        now=system_clock().now,
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True, strip_content=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert len(calls) == 1
    assert dict(os.environ) == before


def test_initialize_skips_when_collector_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: LogRecords,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    """Local collector not answering at init -> recording stays off, no
    Traceloop.init, and a warning event carries the endpoint (the same
    init-time tradeoff the events exporter makes)."""
    home = tmp_path / ".meiva"
    home.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"

    calls: list[dict[str, Any]] = []

    def record_init(**kw: Any) -> None:
        calls.append(kw)

    stub_traceloop_init(monkeypatch, record_init)
    initialize_tracing(
        now=system_clock().now,
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True, endpoint=endpoint),
        readiness=trace_readiness,
    )

    assert calls == []
    assert trace_readiness.ready is False
    warned = warned_fields(loguru_records)
    assert warned
    assert warned[0].get("action") == "recording_disabled_collector_unreachable"
    assert warned[0].get("endpoint") == endpoint
