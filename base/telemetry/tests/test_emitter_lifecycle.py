from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base import telemetry
from base.host import proc
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.delivery import reporting

CHILD = Path(__file__).with_name("_emitter_lifecycle_child.py")


def _run_scenario(scenario: str) -> subprocess.CompletedProcess[str]:
    return proc.run_bounded(
        [sys.executable, "-I", str(CHILD), scenario], capture_output=True, text=True, timeout=10
    )


def event(i: int) -> telemetry.Event:
    return telemetry.Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="log",
        event_name="log",
        level="info",
        source="test",
        target_agent_id=None,
        attributes={"i": i},
    )


def test_process_binding_uses_owned_writer_without_ambient_factory() -> None:
    child = _run_scenario("owned_writer")
    assert child.returncode == 0, child.stderr


# meiva:note
# The old assertion pinned a raise out of `emit_prepared`: it held the process to a behaviour
# that reports an ordinary absence as a broken sink, and that a later edit could turn into a
# failed business write. The new assertion pins the opposite contract — the unbound process
# loses the event, says so exactly once, and names the first event it lost.
# meiva:link base/telemetry/docs/telemetry.meiva.okf.md
def test_emit_before_init_drops_the_event_and_says_so_once() -> None:
    child = _run_scenario("cold_emit")
    assert child.returncode == 0, child.stderr
    assert child.stderr.count("emission skipped: no producer") == 1
    assert "page_ttl_expired" in child.stderr, "the report names the first event it lost"
    assert "requires an initialized process pipeline or producer" not in child.stderr
    assert "emit must not construct a writer" not in child.stderr


# meiva:note
# A client set with no pipeline factory answers through `NoPipelineFactoryError`, and that answer is
# an absence: the metering seam used to route it through `failure_isolated`, which printed a
# full traceback for a process that simply has no stream. One line, no traceback, and the line
# says which cause it was.
# meiva:link base/telemetry/docs/telemetry.meiva.okf.md
def test_emit_without_a_pipeline_factory_drops_and_says_so_once() -> None:
    child = _run_scenario("emit_without_a_factory")
    assert child.returncode == 0, child.stderr
    assert child.stderr.count("emission skipped: no producer") == 1
    assert "no pipeline factory" in child.stderr
    assert "Traceback (most recent call last)" not in child.stderr
    assert "emit failed" not in child.stderr


# meiva:note
# The counter-case that keeps the line above honest: a producer that fails for its own reason is
# a sink failure and must still report with its traceback, or the one-line absence would be
# swallowing real defects.
# meiva:link base/telemetry/docs/telemetry.meiva.okf.md
def test_a_defective_producer_still_reports_a_sink_failure() -> None:
    child = _run_scenario("failing_producer")
    assert child.returncode == 0, child.stderr
    assert "emit failed" in child.stderr
    assert "Traceback (most recent call last)" in child.stderr
    assert "sink defect" in child.stderr
    assert "emission skipped" not in child.stderr


def test_process_binding_rejects_replacing_a_live_owner() -> None:
    child = _run_scenario("replace_live_owner")
    assert child.returncode == 0, child.stderr


def test_full_queue_stop_is_finite_and_closes_admission() -> None:
    entered, release = threading.Event(), threading.Event()
    written: list[telemetry.Event] = []

    def writer(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(3)
        written.extend(batch)

    pipe = telemetry.EventPipeline(writer=writer, batch_size=1, queue_maxsize=1)
    try:
        pipe.enqueue(event(1))
        assert entered.wait(1)
        pipe.enqueue(event(2))
        assert pipe._queue.full()
        assert pipe.stop(timeout=0.03).status is telemetry.DrainStatus.UNFINISHED
        assert not pipe.stopped
        pipe.enqueue(event(3))
        assert pipe._queue.qsize() == 1
        assert pipe.dropped == 1
    finally:
        release.set()
        assert pipe.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED
    assert [e.attributes.get("i") for e in written if e.event_name == "log"] == [1, 2]
    assert not pipe._thread.is_alive()
    assert pipe.stop(timeout=0).status is telemetry.DrainStatus.COMPLETED


def test_sync_deadline_expires_in_marker_while_every_slot_is_taken() -> None:
    entered, release = threading.Event(), threading.Event()

    def writer(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(3)

    pipe = telemetry.EventPipeline(writer=writer, batch_size=1, queue_maxsize=1)
    try:
        pipe.enqueue(event(1))
        assert entered.wait(1)
        pipe.enqueue(event(2))
        started = time.monotonic()
        assert pipe.sync(timeout=0.03) == telemetry.DrainResult(
            telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.MARKER
        )
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        pipe.stop(timeout=1)


def test_sync_deadline_expires_in_drain_while_the_writer_blocks() -> None:
    entered, release = threading.Event(), threading.Event()

    def writer(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(3)

    pipe = telemetry.EventPipeline(writer=writer, batch_size=1, queue_maxsize=2)
    try:
        pipe.enqueue(event(1))
        assert entered.wait(1)

        original_thread = pipe._thread
        before_threads = set(threading.enumerate())
        assert pipe.sync(timeout=0.03) == telemetry.DrainResult(
            telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.DRAIN
        )
        assert pipe._thread is original_thread
        assert set(threading.enumerate()) == before_threads
    finally:
        release.set()
        pipe.stop(timeout=1)


def test_concurrent_sync_receipts_wait_for_their_own_fifo_batch() -> None:
    entered, release = threading.Event(), threading.Event()
    written: list[int] = []

    def writer(batch: list[telemetry.Event]) -> None:
        for emitted in batch:
            index = emitted.attributes["i"]
            assert isinstance(index, int), index
            written.append(index)
        if 2 in written:
            entered.set()
            assert release.wait(3)

    pipe = telemetry.EventPipeline(writer=writer, batch_size=100, flush_interval_s=60)
    try:
        pipe.enqueue(event(1))
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(pipe.sync, 1)
            assert first.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
            pipe.enqueue(event(2))
            second = callers.submit(pipe.sync, 1)
            assert entered.wait(1)
            third = callers.submit(pipe.sync, 1)
            assert not second.done()
            assert not third.done()
            release.set()
            assert second.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
            assert third.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
    finally:
        release.set()
        pipe.stop(timeout=1)
    assert written == [1, 2]


@pytest.mark.parametrize("failure", [RuntimeError("writer defect"), SystemExit("writer exit")])
def test_worker_failure_reports_immediately_and_never_acknowledges(
    failure: BaseException,
    loguru_records: LogRecords,
) -> None:
    def writer(batch: list[telemetry.Event]) -> None:
        raise failure

    pipe = telemetry.EventPipeline(writer=writer, batch_size=100, flush_interval_s=60)
    pipe.enqueue(event(1))
    with pytest.raises(type(failure)) as observed:
        pipe.sync(timeout=1)
    assert observed.value is failure
    assert pipe._finished.wait(1)
    reported_excs = [
        r["exception"].value
        for r in loguru_records
        if r.get("exception") is not None and r["exception"].value is not None
    ]
    assert reported_excs == [failure]
    pipe.enqueue(event(2))
    assert pipe.dropped == 1
    with pytest.raises(type(failure)) as stopped:
        pipe.stop(timeout=1)
    assert stopped.value is failure
    assert not pipe._thread.is_alive()


def test_explicit_sink_isolation_preserves_successful_barrier(
    loguru_records: LogRecords,
) -> None:
    def writer(batch: list[telemetry.Event]) -> None:
        with reporting.failure_isolated("documented exporter"):
            raise RuntimeError("optional sink failure")

    pipe = telemetry.EventPipeline(writer=writer, flush_interval_s=60)
    try:
        pipe.enqueue(event(1))
        assert pipe.sync(timeout=1).status is telemetry.DrainStatus.COMPLETED
        assert any("documented exporter" in msg for msg in loguru_records.messages())
    finally:
        assert pipe.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED


def test_unreleasable_writer_cannot_make_stop_unbounded() -> None:
    child = _run_scenario("unreleasable_writer")
    assert child.returncode == 0, child.stderr
    assert "unfinished" in child.stderr


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan")])
def test_invalid_timeout_cannot_create_an_unbounded_close(timeout: float) -> None:
    pipe = telemetry.EventPipeline(writer=lambda _batch: None)
    try:
        with pytest.raises(ValueError, match="finite and non-negative"):
            pipe.sync(timeout=timeout)
        with pytest.raises(ValueError, match="finite and non-negative"):
            pipe.stop(timeout=timeout)
    finally:
        pipe.stop(timeout=1)


def test_unknown_worker_failure_is_visible_without_a_logging_sink() -> None:
    child = _run_scenario("zero_sink_failure")
    assert child.returncode == 0, child.stderr
    assert "Traceback (most recent call last)" in child.stderr
    assert "RuntimeError: zero-sink worker defect" in child.stderr


def test_binding_reuses_only_joined_successful_owner() -> None:
    child = _run_scenario("reuse_joined_owner")
    assert child.returncode == 0, child.stderr


def test_binding_collects_original_failed_owner_before_replacement() -> None:
    child = _run_scenario("collect_failed_owner")
    assert child.returncode == 0, child.stderr
