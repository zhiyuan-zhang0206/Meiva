from __future__ import annotations

import threading
import time

import pytest

from base.telemetry.otlp.trace_workers import TraceArm


def test_stop_before_start_closes_the_owner_without_launching_a_thread() -> None:
    entered = threading.Event()

    def initialize(_endpoint: str, _stop: threading.Event) -> None:
        entered.set()

    owner = TraceArm("unused", initialize)
    assert owner.stop(timeout=0)
    assert owner.finished.is_set()
    assert not owner.start()
    assert not entered.is_set()


def test_stop_observes_one_real_worker_without_holding_its_completion_lock() -> None:
    entered, release = threading.Event(), threading.Event()

    def initialize(_endpoint: str, stop: threading.Event) -> None:
        entered.set()
        assert release.wait(5)
        assert stop.is_set()

    owner = TraceArm("unused", initialize)
    assert owner.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="already started"):
            owner.start()
        before = time.monotonic()
        assert not owner.stop(timeout=0.01)
        assert time.monotonic() - before < 1
        assert not owner.finished.is_set()
        assert not owner.start()
    finally:
        release.set()
        assert owner.stop(timeout=5)
    assert owner.finished.is_set()


def test_unknown_initializer_error_retains_its_identity_through_stop() -> None:
    failure = BaseException("unknown trace worker defect")

    def initialize(_endpoint: str, _stop: threading.Event) -> None:
        raise failure

    owner = TraceArm("unused", initialize)
    assert owner.start()
    assert owner.finished.wait(5)
    with pytest.raises(BaseException) as raised:
        owner.stop(timeout=5)
    assert raised.value is failure
