"""The event emitter batches, and sheds instead of growing without bound.

Hermetic — no Postgres. The point is the *shape* of the write path, which is
what went missing before: `314708af` added batching to the loguru Postgres
sink, `e92f6e82` removed it three hours later while replaying a stale branch,
and the whole suite stayed green for three weeks because
`base/agents/tests/test_log_sink.py` registered `_event_sink` directly with
`enqueue=False` and never touched `_ThreadedPostgresSink` at all.

The batching now lives in the unified emitter (`base.telemetry`), which the
loguru sink feeds; these tests assert the property the batching exists for —
how many writes N records cost — rather than the presence of a symbol, which a
rename would break and a deletion would not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from base import telemetry
from base.log.tests.log_records import LogRecords
from base.telemetry.delivery import loss, mirror


def _event(i: int, category: str = "log") -> telemetry.Event:
    """A minimal valid event; only identity matters to the batching tests."""
    return telemetry.Event(
        ts=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster=".meiva-test",
        process="test",
        category=category,  # type: ignore[arg-type]
        event_name=f"kind-{i}",
        level="info",
        source="system",
        target_agent_id=None,
        attributes={},
    )


def test_sync_flushes_batch_held_by_drain_thread() -> None:
    """sync() must land a batch the drain thread already dequeued.

    Both flush() and sync() acknowledge the sole writer's held batch before
    returning, including records dequeued before the barrier was admitted.
    """

    rec = _Recorder()
    sink = _make_sink(rec, batch=100, interval=60.0, maxsize=100)
    try:
        sink.enqueue(_event(0))
        # Wait until the drain thread has actually dequeued the record (it
        # holds the batch until its flush_interval — 60s here — so the held
        # batch is stable). A fixed 0.2s sleep could race the drain thread
        # under load and blow the assertion on the wrong side (audit round-2
        # cc-docs-tests P2 — the file's own _drain_sink deadline-polling
        # primitive is the house pattern).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sink._queue.empty():
            time.sleep(0.01)
        assert sink._queue.empty(), "drain thread never dequeued the record"
        assert sink.flush().status is telemetry.DrainStatus.COMPLETED
        sink.sync(timeout=2.0)
        assert [e.event_name for b in rec.batches for e in b] == ["kind-0"]
        sink.sync(timeout=2.0)  # idempotent: nothing new, no extra write
        assert [e.event_name for b in rec.batches for e in b] == ["kind-0"]
    finally:
        sink.stop()


def _drain_sink(sink: Any, expected: int, timeout: float = 5.0) -> None:
    """Wait until `expected` records have reached the writer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sum(len(b) for b in sink._writer.batches) >= expected:
            return
        time.sleep(0.01)


class _Recorder:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def __call__(self, batch: list[Any]) -> None:
        self.batches.append(list(batch))


def _make_sink(
    writer: Callable[[list[Any]], None],
    *,
    batch: int,
    interval: float,
    maxsize: int,
) -> telemetry.EventPipeline:
    return telemetry.EventPipeline(
        writer=writer,
        batch_size=batch,
        flush_interval_s=interval,
        queue_maxsize=maxsize,
    )


# --- the regression: write amplification ---


def test_many_records_cost_few_writes() -> None:
    """100 records must not become 100 round-trips.

    This is the assertion whose absence let the batching be deleted unnoticed.
    """
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=100)
    try:
        for i in range(100):
            sink.enqueue(_event(i))
        _drain_sink(sink, 100)
    finally:
        sink.stop()

    delivered = [m for b in rec.batches for m in b]
    assert len(delivered) == 100, "records were lost"
    assert [m.event_name for m in delivered] == [f"kind-{i}" for i in range(100)], (
        "order was not preserved"
    )
    assert len(rec.batches) <= 20, f"expected batched writes, got {len(rec.batches)}"


def test_single_record_still_lands_within_the_interval() -> None:
    """Batching must not hold a lone record hostage waiting for a full batch."""
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=100)
    try:
        sink.enqueue(_event(1))
        _drain_sink(sink, 1, timeout=2.0)
    finally:
        sink.stop()
    assert [m.event_name for b in rec.batches for m in b] == ["kind-1"]


def test_stop_flushes_what_is_buffered() -> None:
    """A partial batch pending at teardown must not be dropped."""
    rec = _Recorder()
    sink = _make_sink(rec, batch=1000, interval=60.0, maxsize=100)
    for i in range(5):
        sink.enqueue(_event(i))
    time.sleep(0.05)  # let the drain thread pick them up into its batch
    sink.stop()
    assert [m.event_name for b in rec.batches for m in b] == [f"kind-{i}" for i in range(5)]


# --- bounded queue ---


def test_queue_is_bounded_and_counts_what_it_sheds(loguru_records: LogRecords) -> None:
    """A producer outrunning the drain thread sheds records, not memory.

    The drain thread is stalled so the queue genuinely fills; before this bound
    existed the same burst grew the agent process's heap without limit.
    """
    import threading

    blocked = _Recorder()
    release = threading.Event()

    def _stalled(batch: list[Any]) -> None:
        assert release.wait(5.0)
        blocked(batch)

    sink = _make_sink(_stalled, batch=10, interval=0.05, maxsize=20)
    try:
        for i in range(500):
            sink.enqueue(_event(i))
        reports = [record for record in loguru_records if record["extra"].get("queue") == "emitter"]
        assert reports, "expected an error diagnostic after shedding records"
        assert all(record["level"].name == "ERROR" for record in reports)
        assert reports[0]["extra"]["n"] > 0
        assert sink.dropped > 0
        assert sink._queue.qsize() <= 20, "queue grew past its bound"
    finally:
        release.set()
        assert sink.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED


def test_nothing_is_dropped_when_the_drain_keeps_up() -> None:
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=1000)
    try:
        for i in range(50):
            sink.enqueue(_event(i))
        _drain_sink(sink, 50)
    finally:
        sink.stop()
    assert sink.dropped == 0


# --- the batch writer itself ---


# --- production wiring ---


# --- the drop report (ops monitor collection point) ---


def test_shed_records_report_one_event_log_drop() -> None:
    """Loss summaries reach the writer directly even while the queue stays full."""
    import queue
    import threading

    pipe = telemetry.EventPipeline.__new__(telemetry.EventPipeline)
    pipe._queue = queue.Queue(maxsize=1)
    pipe._admission_lock = threading.Lock()
    pipe._stop_requested = threading.Event()
    pipe._finished = threading.Event()
    pipe._dropped_lock = threading.Lock()
    pipe.dropped = 0
    pipe._drop_example = None
    written: list[telemetry.Event] = []
    pipe._writer = written.extend
    pipe.enqueue(_event(0))
    for i in range(1, 6):
        pipe.enqueue(_event(i))
    assert pipe._queue.full()
    pipe._flush([])
    assert pipe._queue.full()
    assert len(written) == 1
    assert written[0].event_name == "event_log_drop"
    assert written[0].attributes["n"] == 5
    assert written[0].level == "error"


# ── emitter self-diagnostics (audit-round2 events-obs P2) ────────────────────


def test_jsonl_mirror_failure_is_reported_not_silent(unit_home: Path) -> None:
    """A JSONL mirror write failure must be REPORTED (first + every 50th), not
    swallowed: the mirror is the durable local copy, and a disk-full /
    permission error would otherwise degrade it without a trace. The report
    carries the `_no_emitter` marker so it never re-enters the pipeline
    (a mirror-down process would otherwise loop failure -> warn -> emit ->
    failure)."""
    from loguru import logger

    captured: list[str] = []
    mirror_dir = unit_home / "logs"
    mirror_dir.mkdir(exist_ok=True)
    day = datetime.now(UTC).strftime("%Y%m%d")
    bad_file = mirror_dir / f"events-{day}.jsonl"
    previous = bad_file.read_bytes() if bad_file.exists() else None
    bad_file.unlink(missing_ok=True)
    bad_file.mkdir()

    initial_failures = mirror.jsonl_failures
    sink_id = logger.add(
        lambda m: captured.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: bool(r["extra"].get("_no_emitter")),
    )
    try:
        for index in range(50):
            telemetry.append_jsonl([_event(index)])
    finally:
        logger.remove(sink_id)
        bad_file.rmdir()
        if previous is not None:
            bad_file.write_bytes(previous)

    expected = [
        count
        for count in range(initial_failures + 1, initial_failures + 51)
        if count == 1 or count % 50 == 0
    ]
    assert mirror.jsonl_failures == initial_failures + 50
    assert len(captured) == len(expected)
    for message, count in zip(captured, expected, strict=True):
        assert f"JSONL mirror write failed ({count} consecutive)" in message


def test_sync_reports_timeout_when_drain_thread_wedged() -> None:
    """sync() must not fail silently when the drain thread cannot land its held
    batch in time — a caller about to TRUNCATE `events` needs to know the
    barrier did not complete (the straggler-flake class this closes)."""
    import threading

    from loguru import logger

    captured: list[str] = []
    gate = threading.Event()

    def _stuck(_batch: list[Any]) -> None:
        gate.wait(timeout=10.0)  # drain thread wedged until the test releases it

    sink_id = logger.add(
        lambda m: captured.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: bool(r["extra"].get("_no_emitter")),
    )
    sink = _make_sink(_stuck, batch=100, interval=60.0, maxsize=100)
    try:
        sink.enqueue(_event(0))
        time.sleep(0.2)  # drain thread dequeues and holds the batch
        sink.sync(timeout=0.2)  # writer never returns -> barrier times out
        assert any("sync() timed out" in m for m in captured)
    finally:
        gate.set()  # release the drain thread so stop() can join
        sink.stop()
        logger.remove(sink_id)


def test_delayed_loss_summary_preserves_actual_drop_time() -> None:
    from datetime import timedelta

    dropped_at = datetime.now(UTC) - timedelta(minutes=10)
    report = loss.loss_event(_event(1), 3, "emitter", dropped_at=dropped_at)
    assert report.attributes["last_dropped_at"] == dropped_at.timestamp()
    assert report.ts > dropped_at
