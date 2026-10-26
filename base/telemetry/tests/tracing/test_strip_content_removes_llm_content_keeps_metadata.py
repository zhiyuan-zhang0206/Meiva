"""Tracing cases: strip content removes llm content keeps metadata."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.paths import traces_dir
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
    from base.telemetry.tracing import strip_content_attributes

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
    from base.telemetry.tracing import MAX_ATTR_STRING_CHARS, strip_content_attributes

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
    from base.telemetry.tracing import strip_content_attributes

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
    stripped here, content never reaches the sidecar, the mirror or Tempo
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


def test_exporter_strip_opt_out_keeps_content(collector_up: CollectorStubServer) -> None:
    """trace_strip_content=False opts the wire back into full content
    (benchmarks that genuinely want prompts in Tempo/mirror)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(OtlpJsonHttpSpanExporter(collector_up.endpoint, strip_content=False))
    )
    tracer = provider.get_tracer("meiva.session")
    with tracer.start_as_current_span("root"):
        pass
    provider.shutdown()

    assert collector_up.posts


def test_enforce_dir_cap_deletes_oldest_first(tmp_path: Path) -> None:
    """enforce_dir_cap deletes oldest files until the directory fits the cap."""
    from base.telemetry.tracing import enforce_dir_cap

    # 4 files of 1 MB each: one rotated backup per day, 2026-01-01 .. 2026-01-04
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        (tmp_path / f"spans-{day}T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=2, traces_dir=tmp_path)  # cap 2 MB -> delete 2 oldest
    assert removed == 2
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == [
        "spans-2026-01-03T00-00-00.000-size.jsonl",
        "spans-2026-01-04T00-00-00.000-size.jsonl",
    ]


def test_enforce_dir_cap_noop_when_under_cap(tmp_path: Path) -> None:
    """Under the cap nothing is deleted; non-positive cap disables entirely."""
    from base.telemetry.tracing import enforce_dir_cap

    (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl").write_bytes(b"x" * 10)
    assert enforce_dir_cap(max_mb=100, traces_dir=tmp_path) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1
    assert enforce_dir_cap(max_mb=0, traces_dir=tmp_path) == 0
    assert len(list(tmp_path.glob("spans*.jsonl"))) == 1


def test_enforce_dir_cap_active_file_sorts_last(tmp_path: Path) -> None:
    """The ACTIVE spans.jsonl (no day stamp) sorts last — the cap prune never
    deletes the file the collector is appending to."""
    from base.telemetry.tracing import enforce_dir_cap

    (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))
    (tmp_path / "spans-2026-01-02T03-04-05.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))
    active = tmp_path / "spans.jsonl"
    active.write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=2, traces_dir=tmp_path)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    assert remaining == ["spans-2026-01-02T03-04-05.000-size.jsonl", "spans.jsonl"], (
        "the active file must survive a cap prune"
    )


def test_disk_watermark_exceeded(tmp_path: Path) -> None:
    """disk_watermark_exceeded compares the data-disk usage fraction against
    the watermark; >= 1.0 disables the guard."""
    from base.telemetry.tracing import disk_watermark_exceeded

    sample_usage = (50 / 1000, 950 * 4096)
    assert disk_watermark_exceeded(0.9, traces_dir=tmp_path, usage=sample_usage) is False
    assert disk_watermark_exceeded(0.01, traces_dir=tmp_path, usage=sample_usage) is True
    assert disk_watermark_exceeded(1.0, traces_dir=tmp_path, usage=sample_usage) is False
    assert disk_watermark_exceeded(2.0, traces_dir=tmp_path, usage=sample_usage) is False


def test_initialize_relief_pass_runs_when_disk_over_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, trace_readiness: TraceReadiness
) -> None:
    """The bounded-disk pass (gzip / retention / cap) runs BEFORE the
    watermark guard: an over-watermark disk still gets its relief pass — the
    stale segment is pruned, the gzip and cap legs are invoked, and recording
    itself is skipped (auto-degrade, watermark guard)."""
    home = tmp_path / ".meiva"
    home.mkdir()
    mirror = traces_dir(home)
    mirror.mkdir()
    old = mirror / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    recent_older = mirror / "spans-2099-01-01T00-00-00.000-size.jsonl"
    recent_newer = mirror / "spans-2099-01-02T00-00-00.000-size.jsonl"
    recent_older.write_bytes(os.urandom(700 * 1024))
    recent_newer.write_bytes(os.urandom(700 * 1024))

    init_calls: list[dict[str, object]] = []

    def record_init(**kw: object) -> None:
        init_calls.append(kw)

    stub_traceloop_init(monkeypatch, record_init)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(
            enabled=True,
            strip_content=True,
            retention_days=14,
            max_dir_mb=1,
            disk_watermark=0.9,
            gzip_grace_seconds=-1,
        ),
        readiness=trace_readiness,
        disk_stats=(0.99, 10 * 1024**3),
    )

    # meiva:note
    # The relief pass ran: `old` was pruned, `recent_older` and `recent_newer`
    # were gzipped, `recent_older.gz` was removed by the 1 MB cap, and
    # recording itself was skipped due to the disk watermark.
    assert not old.exists()
    assert not recent_older.exists()
    assert not (mirror / f"{recent_older.name}.gz").exists()
    assert (mirror / f"{recent_newer.name}.gz").exists()
    assert init_calls == []


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


def test_initialize_skips_when_disk_over_watermark(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_records: LogRecords,
    *,
    trace_readiness: TraceReadiness,
) -> None:
    """Disk over watermark: recording stays off, no Traceloop.init, and a
    warning telemetry event is emitted carrying the measured numbers."""
    home = tmp_path / ".meiva"
    home.mkdir()
    calls: list[dict[str, Any]] = []

    def record_init(**kw: Any) -> None:
        calls.append(kw)

    stub_traceloop_init(monkeypatch, record_init)
    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True, disk_watermark=0.9),
        readiness=trace_readiness,
        disk_stats=(0.951, 2 * 1024**3),
    )

    assert calls == []
    assert trace_readiness.ready is False
    warned = warned_fields(loguru_records)
    assert warned, "a degradation warning must be logged"
    attrs = warned[0]
    assert attrs.get("event") == "trace"
    assert attrs.get("action") == "recording_disabled_disk_watermark"
    assert attrs.get("usage_fraction") == 0.951
    assert attrs.get("free_gb") == 2.0


def test_enforce_dir_cap_survives_peer_prune(tmp_path: Path) -> None:
    """A file vanishing between glob and stat — a peer agent pruning the shared
    traces dir concurrently — must not raise out of the boot path (audit
    2026-08-08 P1: two bare p.stat() calls killed an agent start with
    FileNotFoundError)."""
    from base.telemetry.tracing import enforce_dir_cap

    (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))
    vanished = tmp_path / "spans-2026-01-02T00-00-00.000-size.jsonl"
    vanished.symlink_to(tmp_path / "vanished-peer-segment")
    (tmp_path / "spans-2026-01-03T00-00-00.000-size.jsonl").write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=1, traces_dir=tmp_path)  # must not raise
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl"))
    # the vanished file counted as 0 bytes; the sweep continued past it and
    # deleted the oldest surviving file
    assert remaining == [
        "spans-2026-01-02T00-00-00.000-size.jsonl",
        "spans-2026-01-03T00-00-00.000-size.jsonl",
    ]
