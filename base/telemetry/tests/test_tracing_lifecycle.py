from __future__ import annotations

import threading
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.telemetry import tracing
from base.telemetry.otlp.trace_readiness import TraceReadiness
from base.telemetry.otlp.trace_workers import CollectorRetry, TraceArm
from base.telemetry.tests.capture.trace_process import run_trace_scenario
from base.telemetry.tests.otlp.identity_inputs import machine_identity
from base.telemetry.tests.test_tracing import trace_config


def test_first_use_timeout_still_allows_late_arm_success(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "late-success", lifecycle=True)


def test_real_sdk_arm_block_does_not_hold_stop_lock(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "shutdown", lifecycle=True)


def test_unknown_late_arm_error_retains_identity_after_an_unfinished_stop() -> None:
    entered, release = threading.Event(), threading.Event()
    failure = BaseException("unexpected late owner defect")

    def initialize(_endpoint: str, _stop: threading.Event) -> None:
        entered.set()
        assert release.wait(5)
        raise failure

    owner = TraceArm("unused", initialize)
    assert owner.start()
    try:
        assert entered.wait(5)
        assert not owner.stop(timeout=0.01)
    finally:
        release.set()
        assert owner.finished.wait(5)
        with pytest.raises(BaseException) as raised:
            owner.stop(timeout=5)
        assert raised.value is failure


def test_collector_wait_does_not_run_preflight_and_stop_wakes_it(tmp_path: Path) -> None:
    receipt = tmp_path / "preflight"
    owner = CollectorRetry(
        initialize=receipt.touch,
        resolved=receipt.exists,
        interval=lambda: 300.0,
    )
    try:
        assert not receipt.exists()
        assert owner.stop(timeout=1)
        assert owner.finished.is_set()
        assert not receipt.exists()
    finally:
        owner.stop(timeout=5)


def test_retry_unknown_error_retains_identity() -> None:
    failure = BaseException("unexpected retry preflight")

    def initialize() -> None:
        raise failure

    owner = CollectorRetry(initialize=initialize, resolved=lambda: False, interval=lambda: 0)
    assert owner.finished.wait(5)
    with pytest.raises(BaseException) as raised:
        owner.stop(timeout=5)
    assert raised.value is failure


def test_trace_identity_defect_fails_before_sdk_admission(tmp_path: Path) -> None:
    readiness = TraceReadiness()
    facts = machine_identity()

    def invalid() -> str:
        raise RuntimeError("invalid machine reader")

    machine = MachineIdentity(
        name=invalid,
        role=facts.role,
        host=facts.host,
        description=facts.description,
    )
    with pytest.raises(RuntimeError, match="invalid machine reader"):
        tracing.start_arm_thread(trace_config(), machine, tmp_path, readiness=readiness)
    assert not readiness.resolved
    assert readiness.read(timeout=0).first_timeout is False


def test_stopped_synchronous_initializer_cancels_before_sdk_import(tmp_path: Path) -> None:
    run_trace_scenario(tmp_path, "cancelled-sync", lifecycle=True)
