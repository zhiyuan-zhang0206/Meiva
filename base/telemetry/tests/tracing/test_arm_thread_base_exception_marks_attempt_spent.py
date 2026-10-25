"""Tracing cases: arm thread base exception marks attempt spent."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry import tracing as tracing_mod
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.tests.capture.trace_process import run_trace_scenario
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tests.test_tracing import (
    CollectorStubServer,
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

    monkeypatch.setattr("traceloop.sdk.Traceloop.init", _exit_sdk)

    initialize_tracing(
        machine=machine_identity(),
        home=home,
        config=trace_config(enabled=True),
        readiness=trace_readiness,
    )
    wait_init_resolved(trace_readiness=trace_readiness)

    assert trace_readiness.resolved and not trace_readiness.ready
    assert trace_readiness.ready is False

    initialize_tracing(
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


def test_turn_span_exports_root_at_start_not_at_end(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "root")


def test_prune_old_mirror_removes_stale_keeps_recent(tmp_path: Path) -> None:
    """prune_old_mirror deletes the collector's rotated `spans-<ISO>-size.jsonl`
    files older than retention_days, keeps recent ones, and never touches the
    unstamped ACTIVE `spans.jsonl` or non-mirror files."""
    from base.telemetry.tracing import prune_old_mirror

    old = tmp_path / "spans-2020-01-01T00-00-00.000-size.jsonl"  # well before any cutoff
    old_gz = tmp_path / "spans-2020-01-01T01-00-00.000-time.jsonl.gz"  # same, gzipped
    recent = tmp_path / "spans-2099-01-01T00-00-00.000-size.jsonl"  # well after any cutoff
    active = tmp_path / "spans.jsonl"  # the collector's active file: never pruned
    other = tmp_path / ".ship-watermark.json"  # not a mirror file
    for p in (old, old_gz, recent, active, other):
        p.write_text("{}\n", encoding="utf-8")

    prune_old_mirror(retention_days=14, traces_dir=tmp_path)

    assert not old.exists()
    assert not old_gz.exists()
    assert recent.exists()
    assert active.exists()
    assert other.exists()


def test_mirror_day_parses_all_collector_name_shapes(tmp_path: Path) -> None:
    """`mirror_day` parses every mirror filename shape in the wild:
    collector-rotated backups with the timberjack trigger suffix (`-size` /
    `-time`), manual `spans.cut-*` orphans, and the `.gz` variants of each —
    while the unstamped ACTIVE `spans.jsonl` stays None (never a prune target)."""
    from datetime import date

    from base.telemetry.trace_mirror import mirror_day

    cases = {
        "spans-2026-08-27T03-29-10.942-size.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-time.jsonl": date(2026, 8, 27),
        "spans-2026-08-27T03-29-10.942-size.jsonl.gz": date(2026, 8, 27),
        "spans.cut-20260827.jsonl": date(2026, 8, 27),
        "spans.cut-20260827.jsonl.gz": date(2026, 8, 27),
        "spans.jsonl": None,
        "spans.jsonl.gz": None,
        ".ship-watermark.json": None,
    }
    for name, expected in cases.items():
        p = tmp_path / name
        p.touch()
        assert mirror_day(p) == expected, name


def test_mirror_sort_key_orders_suffixed_rotated_and_cut_files(
    tmp_path: Path,
) -> None:
    """The cap-prune order key handles timberjack-suffixed backups and manual
    cuts (day from the name, sub-day epoch from the timestamp), so a cap prune
    deletes them oldest-first instead of treating them like the active file."""
    from base.telemetry.trace_mirror import mirror_sort_key

    names = [
        "spans-2026-08-01T00-00-00.000-size.jsonl",
        "spans-2026-08-01T01-00-00.000-time.jsonl",
        "spans.cut-20260802.jsonl",
        "spans.jsonl",
    ]
    keys = [mirror_sort_key(tmp_path / n) for n in names]
    # Both 08-01 segments before the 08-02 cut, all before the active file.
    assert keys == sorted(keys)


def test_prune_old_mirror_removes_stale_suffixed_and_gz(tmp_path: Path) -> None:
    """prune_old_mirror deletes stale files regardless of the rotation
    naming era — timberjack-suffixed (`-size`), manual cuts, and gzipped
    segments — and keeps the ACTIVE `spans.jsonl` untouched."""
    from base.telemetry.tracing import prune_old_mirror

    old_suffixed = tmp_path / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old_gz = tmp_path / "spans-2020-01-01T01-00-00.000-time.jsonl.gz"
    old_cut = tmp_path / "spans.cut-20200101.jsonl"
    recent = tmp_path / "spans-2099-01-01T00-00-00.000-size.jsonl"
    active = tmp_path / "spans.jsonl"
    for p in (old_suffixed, old_gz, old_cut, recent, active):
        p.write_text("{}\n", encoding="utf-8")

    prune_old_mirror(retention_days=14, traces_dir=tmp_path)

    assert not old_suffixed.exists()
    assert not old_gz.exists()
    assert not old_cut.exists()
    assert recent.exists()
    assert active.exists()


def test_enforce_dir_cap_counts_suffixed_and_gz_keeps_active(tmp_path: Path) -> None:
    """The cap prune counts suffixed rotated and gzipped files toward the
    directory size and deletes oldest-first, and never deletes the ACTIVE
    `spans.jsonl` even when every other file carries an unrecognized-era
    name (the pre-fix bug: suffixed backups sorted with the active file and
    could be deleted in either order)."""
    from base.telemetry.tracing import enforce_dir_cap

    oldest = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    newest = tmp_path / "spans-2026-01-02T00-00-00.000-time.jsonl.gz"
    active = tmp_path / "spans.jsonl"
    for p in (oldest, newest, active):
        p.write_bytes(b"x" * (1024 * 1024))

    removed = enforce_dir_cap(max_mb=2, traces_dir=tmp_path)
    assert removed == 1
    remaining = sorted(p.name for p in tmp_path.glob("spans*.jsonl*"))
    assert remaining == ["spans-2026-01-02T00-00-00.000-time.jsonl.gz", "spans.jsonl"]


def test_gzip_old_mirror_compresses_rotated_keeps_active(tmp_path: Path) -> None:
    """gzip_old_mirror compresses every non-active mirror file (rotated,
    suffixed, cut) to `.jsonl.gz` with lossless content, leaves the
    ACTIVE `spans.jsonl` alone, and is idempotent on re-run."""
    import gzip as gz

    from base.telemetry.tracing import gzip_old_mirror

    rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    cut = tmp_path / "spans.cut-20260827.jsonl"
    active = tmp_path / "spans.jsonl"
    rotated.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")
    cut.write_text('{"b":1}\n', encoding="utf-8")
    active.write_text('{"c":1}\n', encoding="utf-8")

    assert gzip_old_mirror(grace_seconds=-1, traces_dir=tmp_path) == 2
    assert rotated.exists() is False
    assert cut.exists() is False
    gz_rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz"
    gz_cut = tmp_path / "spans.cut-20260827.jsonl.gz"
    assert gz_rotated.exists()
    assert gz_cut.exists()
    assert active.exists()  # never compressed
    assert not (tmp_path / "spans.jsonl.gz").exists()
    with gz.open(gz_rotated, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"a":1}\n{"a":2}\n'
    with gz.open(gz_cut, "rt", encoding="utf-8") as fh:
        assert fh.read() == '{"b":1}\n'
    # Idempotent: already-compressed files are skipped.
    assert gzip_old_mirror(grace_seconds=-1, traces_dir=tmp_path) == 0


def test_gzip_old_mirror_skips_recently_written_files(tmp_path: Path) -> None:
    """A file written within the grace window is skipped (a freshly cut
    active file may still be appended by the collector until its next size
    rotation); the next pass with no grace compresses it."""
    import time

    from base.telemetry.tracing import gzip_old_mirror

    fresh = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    fresh.write_text("x\n", encoding="utf-8")
    # Touch mtime to "now" (write_text already did; keep explicit for clarity).
    now = time.time()
    import os

    os.utime(fresh, (now, now))

    assert gzip_old_mirror(grace_seconds=3600, traces_dir=tmp_path) == 0
    assert fresh.exists()
    assert not (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz").exists()

    assert gzip_old_mirror(grace_seconds=-1, traces_dir=tmp_path) == 1
    assert not fresh.exists()


def test_prune_old_mirror_disabled_when_nonpositive(tmp_path: Path) -> None:
    """retention_days <= 0 disables pruning entirely."""
    from base.telemetry.tracing import prune_old_mirror

    old = tmp_path / "spans-2020-01-01T00-00-00.000-size.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    prune_old_mirror(retention_days=0, traces_dir=tmp_path)
    assert old.exists()


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


def test_gzip_old_mirror_failure_is_reported_not_swallowed(
    tmp_path: Path, loguru_records: list[dict]
) -> None:
    from base.telemetry.tracing import gzip_old_mirror

    rotated = tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl"
    rotated.write_text('{"a":1}\n', encoding="utf-8")
    tmp_path.chmod(0o500)
    try:
        assert gzip_old_mirror(grace_seconds=-1, traces_dir=tmp_path) == 0
    finally:
        tmp_path.chmod(0o700)

    assert rotated.exists(), "the file stays for the next pass to retry"
    assert not (tmp_path / "spans-2026-01-01T00-00-00.000-size.jsonl.gz").exists()
    [record] = loguru_records
    assert "trace mirror gzip" in record["message"]
