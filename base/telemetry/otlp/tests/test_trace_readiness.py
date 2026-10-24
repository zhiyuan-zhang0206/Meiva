from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from base.telemetry.otlp.trace_readiness import RecordingReading, TraceReadiness


def test_no_admission_has_no_wait_and_cannot_publish_a_result() -> None:
    readiness = TraceReadiness()

    assert readiness.read(timeout=0) == RecordingReading(ready=False, first_timeout=False)
    assert not readiness.resolved
    with pytest.raises(RuntimeError, match="no outstanding admission"):
        readiness.complete(recording=True)
    with pytest.raises(RuntimeError, match="no outstanding admission"):
        readiness.begin()


@pytest.mark.parametrize("recording", [True, False])
def test_one_reservation_has_one_completion(recording: bool) -> None:
    readiness = TraceReadiness()

    assert readiness.reserve()
    assert not readiness.reserve()
    assert readiness.begin()
    readiness.complete(recording=recording)

    assert readiness.resolved
    assert readiness.ready is recording
    assert readiness.read(timeout=0) == RecordingReading(ready=recording, first_timeout=False)
    assert not readiness.reserve()
    with pytest.raises(RuntimeError, match="no outstanding admission"):
        readiness.complete(recording=recording)


def test_close_before_admission_cannot_be_reopened() -> None:
    readiness = TraceReadiness()

    readiness.close()

    assert readiness.resolved
    assert not readiness.reserve()
    assert readiness.read(timeout=0) == RecordingReading(ready=False, first_timeout=False)


def test_pending_admission_can_be_cancelled_without_an_initializer_result() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    with pytest.raises(RuntimeError, match="no outstanding admission"):
        readiness.complete(recording=False)

    readiness.close()

    assert readiness.resolved
    assert not readiness.begin()
    assert not readiness.reserve()
    assert readiness.read(timeout=0) == RecordingReading(ready=False, first_timeout=False)


def test_close_wakes_a_pending_reader_without_spending_timeout_diagnostic() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    entered = threading.Event()

    def observe() -> RecordingReading:
        entered.set()
        return readiness.read(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        observer = pool.submit(observe)
        try:
            assert entered.wait(5)
            with pytest.raises(TimeoutError):
                observer.result(timeout=0.02)
            readiness.close()
            assert observer.result(timeout=5) == RecordingReading(False, False)
        finally:
            readiness.close()


def test_duplicate_begin_is_an_invariant_error_not_a_second_initializer() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    assert readiness.begin()
    try:
        with pytest.raises(RuntimeError):
            readiness.begin()
    finally:
        readiness.complete(recording=False)


def test_concurrent_begin_and_close_preserve_the_actual_initializer_result() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    competing = threading.Barrier(2, timeout=5)
    closed = threading.Event()

    def initialize() -> bool:
        competing.wait()
        began = readiness.begin()
        if began:
            try:
                assert closed.wait(5)
            finally:
                readiness.complete(recording=True)
        return began

    def close() -> None:
        competing.wait()
        try:
            readiness.close()
        finally:
            closed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        initializer = pool.submit(initialize)
        closer = pool.submit(close)
        try:
            closer.result(timeout=5)
            began = initializer.result(timeout=5)
            assert readiness.resolved
            assert readiness.ready is began
            assert not readiness.reserve()
        finally:
            closed.set()
            readiness.close()


def test_close_does_not_claim_a_running_initializer_finished() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()

    assert readiness.begin()
    readiness.close()

    assert not readiness.resolved
    assert not readiness.reserve()
    readiness.complete(recording=False)
    assert readiness.resolved
    assert not readiness.ready


def test_timeout_does_not_cancel_late_success_or_repeat_the_wait() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()

    assert readiness.read(timeout=0) == RecordingReading(ready=False, first_timeout=True)
    assert not readiness.resolved
    assert readiness.read(timeout=0) == RecordingReading(ready=False, first_timeout=False)
    assert readiness.begin()
    readiness.complete(recording=True)
    assert readiness.read(timeout=0) == RecordingReading(ready=True, first_timeout=False)


@pytest.mark.parametrize("recording", [True, False])
def test_all_waiters_observe_the_same_real_completion(recording: bool) -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    waiting = threading.Barrier(3, timeout=5)

    def read() -> RecordingReading:
        waiting.wait()
        return readiness.read(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        observers = [pool.submit(read), pool.submit(read)]
        try:
            waiting.wait()
            for observer in observers:
                with pytest.raises(TimeoutError):
                    observer.result(timeout=0.02)
            assert readiness.begin()
            readiness.complete(recording=recording)
            assert [observer.result(timeout=5) for observer in observers] == [
                RecordingReading(ready=recording, first_timeout=False)
            ] * 2
        finally:
            readiness.close()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_wait_budget_is_validated_before_use(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        TraceReadiness().read(timeout=timeout)


def test_closing_admission_preserves_an_already_published_success() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    assert readiness.begin()
    readiness.complete(recording=True)

    readiness.close()

    assert readiness.read(timeout=0) == RecordingReading(ready=True, first_timeout=False)
    assert not readiness.reserve()


def test_concurrent_reservations_admit_only_one_real_attempt() -> None:
    readiness = TraceReadiness()
    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = [pool.submit(readiness.reserve) for _ in range(8)]
        assert sum(result.result(timeout=5) for result in reservations) == 1
    assert readiness.begin()
    readiness.complete(recording=False)


def test_concurrent_timeouts_claim_the_diagnostic_once() -> None:
    readiness = TraceReadiness()
    assert readiness.reserve()
    with ThreadPoolExecutor(max_workers=4) as pool:
        observations = [pool.submit(readiness.read, timeout=0) for _ in range(8)]
        assert sum(result.result(timeout=5).first_timeout for result in observations) == 1
    assert not readiness.resolved
    assert readiness.begin()
    readiness.complete(recording=True)
