from __future__ import annotations

import _imp
import faulthandler
import sys
import threading
import time
from collections.abc import Generator
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from types import FrameType

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from base.cluster.machine import MachineFacts
from base.paths import meiva_home
from base.telemetry import tracing
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING, RecordingReading
from base.telemetry.otlp.trace_workers import TraceArm
from base.telemetry.tests.capture.armed_tracing import collector_endpoint, stop_provider


def enter_turn() -> bool:
    with tracing.turn_span(name="first turn", session_id="42", turn=1):
        return trace.get_current_span().get_span_context().is_valid


def actual_provider() -> TracerProvider | None:
    provider = trace.get_tracer_provider()
    return provider if isinstance(provider, TracerProvider) else None


def no_admission() -> None:
    assert PROCESS_RECORDING.read(timeout=5) == RecordingReading(False, False)
    assert not enter_turn()
    assert not PROCESS_RECORDING.resolved
    assert "traceloop.sdk" not in sys.modules


def cancelled_sync_arm() -> None:
    stopped = threading.Event()
    stopped.set()
    tracing.arm_tracing(
        "http://127.0.0.1:1",
        stopped,
        production=False,
        home=meiva_home(),
        strip_content=False,
        readiness=PROCESS_RECORDING,
    )
    assert PROCESS_RECORDING.resolved
    assert PROCESS_RECORDING.read(timeout=5) == RecordingReading(False, False)
    assert not PROCESS_RECORDING.reserve()
    assert "traceloop.sdk" not in sys.modules


def observe_first_turn(pool: ThreadPoolExecutor) -> Future[bool]:
    entered = threading.Event()

    def caller() -> bool:
        entered.set()
        return enter_turn()

    turn = pool.submit(caller)
    assert entered.wait(5), "actual first-turn caller did not start"
    try:
        turn.result(timeout=0.02)
    except TimeoutError:
        return turn
    raise AssertionError("first turn bypassed the admitted SDK arm")


def observe_late_success() -> None:
    assert PROCESS_RECORDING.read(timeout=0.01) == RecordingReading(False, True)
    assert PROCESS_RECORDING.read(timeout=0) == RecordingReading(False, False)
    assert not enter_turn()


def observe_competing_arms(pool: ThreadPoolExecutor, endpoint: str, owner: TraceArm) -> None:
    competing = [
        pool.submit(
            tracing.arm_tracing,
            endpoint,
            production=False,
            home=meiva_home(),
            strip_content=False,
            readiness=PROCESS_RECORDING,
        )
        for _ in range(2)
    ]
    assert [attempt.result(timeout=5) for attempt in competing] == [None, None]
    assert tracing.state["arm_owner"] is owner


def observe_running_shutdown() -> None:
    deadline = time.monotonic() + 15
    while not arm_is_waiting_for_import():
        assert time.monotonic() < deadline, "real initializer did not enter SDK import"
        threading.Event().wait(0.001)
    before = time.monotonic()
    assert tracing.shutdown(timeout=0.01, readiness=PROCESS_RECORDING) == ("trace-arm",)
    assert time.monotonic() - before < 1
    assert not PROCESS_RECORDING.resolved


def arm_is_waiting_for_import() -> bool:
    frames = sys._current_frames()
    for thread in threading.enumerate():
        identity = thread.ident
        if thread.name != "trace-arm" or identity is None:
            continue
        frame: FrameType | None = frames.get(identity)
        observed: set[tuple[str, str]] = set()
        while frame is not None:
            observed.add((frame.f_code.co_name, frame.f_code.co_filename))
            frame = frame.f_back
        return any(name == "_arm_tracing" for name, _ in observed) and any(
            filename == "<frozen importlib._bootstrap>" for _, filename in observed
        )
    return False


@contextmanager
def blocked_sdk_import() -> Generator[None]:
    _imp.acquire_lock()
    try:
        yield
    finally:
        _imp.release_lock()


def observe_pending(
    scenario: str,
    pool: ThreadPoolExecutor,
    endpoint: str,
    owner: TraceArm,
) -> Future[bool] | None:
    if scenario == "first-turn":
        return observe_first_turn(pool)
    if scenario == "late-success":
        observe_late_success()
    elif scenario == "concurrent":
        observe_competing_arms(pool, endpoint, owner)
    elif scenario == "shutdown":
        observe_running_shutdown()
    else:
        raise ValueError(f"Unknown pending trace scenario: {scenario!r}")
    return None


def verify_recorded_turn() -> None:
    provider = actual_provider()
    assert provider is not None
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert enter_turn()
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["first turn"]
    assert spans[0].end_time is not None


def diagnostic(stage: str) -> None:
    sys.stderr.write(f"trace lifecycle {stage} t={time.monotonic()}\n")
    sys.stderr.flush()


def pending(scenario: str) -> None:
    with collector_endpoint() as endpoint, ThreadPoolExecutor(max_workers=2) as pool:
        try:
            # meiva:note
            # Hold the real interpreter import resource, never replace an initializer or
            # expose tracing's private SDK lock. Failure releases it before observing stop.
            with blocked_sdk_import():
                tracing.start_arm_thread(
                    TraceConfig(True, False, endpoint, 3, 2048, 0.9, True),
                    MachineFacts.of(name="tracing-test", role={"agent-runner"}),
                    meiva_home(),
                    readiness=PROCESS_RECORDING,
                )
                owner = tracing.state["arm_owner"]
                assert isinstance(owner, TraceArm)
                diagnostic("admitted")
                turn = observe_pending(scenario, pool, endpoint, owner)
            diagnostic("released SDK lock")
            if not owner.finished.wait(20):
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                raise AssertionError("actual SDK initializer did not finish")
            diagnostic("worker finished")
            if scenario == "shutdown":
                assert PROCESS_RECORDING.resolved and not PROCESS_RECORDING.ready
                assert tracing.shutdown(timeout=5, readiness=PROCESS_RECORDING) == ()
            else:
                assert PROCESS_RECORDING.ready
                if turn is not None:
                    assert turn.result(timeout=5)
                verify_recorded_turn()
        finally:
            try:
                assert tracing.shutdown(timeout=20, readiness=PROCESS_RECORDING) == ()
            finally:
                stop_provider(actual_provider())


def main(scenario: str) -> None:
    if scenario == "no-admission":
        no_admission()
    elif scenario == "cancelled-sync":
        cancelled_sync_arm()
    else:
        pending(scenario)


if __name__ == "__main__":
    diagnostic(f"started scenario={sys.argv[1]}")
    faulthandler.dump_traceback_later(10, file=sys.stderr)
    try:
        main(sys.argv[1])
    finally:
        faulthandler.cancel_dump_traceback_later()
