from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.telemetry.delivery.receipts import DrainPhase, DrainResult, DrainStatus, Event
from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_exporting
from base.telemetry.tests.otlp.test_telemetry_otlp import otlp_test_event


class _Instrument:
    def __init__(self, on_metric: Callable[[int | float], None] | None = None) -> None:
        self._on_metric = on_metric

    def add(self, value: int | float, _attributes: Any = None) -> None:
        if self._on_metric is not None:
            self._on_metric(value)

    def record(self, value: int | float, _attributes: Any = None) -> None:
        if self._on_metric is not None:
            self._on_metric(value)


class _Meter:
    def __init__(self, on_metric: Callable[[int | float], None] | None = None) -> None:
        self._on_metric = on_metric

    def create_counter(self, _name: str, **_kwargs: Any) -> _Instrument:
        return _Instrument(self._on_metric)

    def create_histogram(self, _name: str, **_kwargs: Any) -> _Instrument:
        return _Instrument(self._on_metric)

    def create_observable_gauge(self, _name: str, **_kwargs: Any) -> object:
        return object()


class _Logger:
    def __init__(self, on_emit: Callable[[Any], None] | None = None) -> None:
        self._on_emit = on_emit

    def emit(self, record: Any) -> None:
        if self._on_emit is not None:
            self._on_emit(record)


class _Provider:
    def __init__(
        self,
        *,
        on_emit: Callable[[Any], None] | None = None,
        on_get_meter: Callable[[], object] | None = None,
        on_metric: Callable[[int | float], None] | None = None,
        on_flush: Callable[[int], None] | None = None,
        on_shutdown: Callable[[], None] | None = None,
    ) -> None:
        self.closed = threading.Event()
        self.flushed = threading.Event()
        self._on_emit = on_emit
        self._on_get_meter = on_get_meter
        self._on_metric = on_metric
        self._on_flush = on_flush
        self._on_shutdown = on_shutdown

    def get_logger(self, _name: str) -> _Logger:
        return _Logger(self._on_emit)

    def get_meter(self, _name: str) -> object:
        if self._on_get_meter is not None:
            return self._on_get_meter()
        return _Meter(self._on_metric)

    def force_flush(self, *, timeout_millis: int) -> None:
        if self._on_flush is not None:
            self._on_flush(timeout_millis)
        self.flushed.set()

    def shutdown(self) -> None:
        if self._on_shutdown is not None:
            self._on_shutdown()
        self.closed.set()


class _BlockingProvider(_Provider):
    def __init__(self) -> None:
        super().__init__()
        self.teardown_entered = threading.Event()
        self.teardown_entered_at = 0.0
        self.release = threading.Event()

    def shutdown(self) -> None:
        self.teardown_entered_at = time.monotonic()
        self.teardown_entered.set()
        assert self.release.wait(5)
        self.closed.set()


class _OkProbeHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        pass


def _backend(
    *,
    logs: _Provider | None = None,
    metrics: _Provider | None = None,
    enabled: Callable[[], bool] = lambda: True,
    queue_maxsize: int = 4096,
    defer_enabled: bool = True,
) -> tuple[telemetry_otlp.OtlpBackend, _Provider, _Provider]:
    resolved_logs = _Provider() if logs is None else logs
    resolved_metrics = _Provider() if metrics is None else metrics
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=enabled,
        providers=(resolved_logs, resolved_metrics),
        queue_maxsize=queue_maxsize,
        defer_enabled=defer_enabled,
    )
    return backend, resolved_logs, resolved_metrics


def _assert_release_completes(pipeline: Any, release: threading.Event) -> None:
    release.set()
    assert pipeline.stop(timeout=2).status is DrainStatus.COMPLETED
    assert pipeline.stopped


def _logged_exceptions(loguru_records: LogRecords) -> list[BaseException]:
    return [r["exception"].value for r in loguru_records if r["exception"] is not None]


def test_blocked_sdk_init_stays_owned_after_finite_stop() -> None:
    entered, release = threading.Event(), threading.Event()

    def get_meter() -> object:
        entered.set()
        assert release.wait(5)
        return _Meter()

    backend, logs, metrics = _backend(metrics=_Provider(on_get_meter=get_meter))
    worker = backend._get_worker()
    assert worker is not None
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert time.monotonic() - started < 1
        assert worker._thread.is_alive()
        assert backend._worker is worker
        backend.export_batch([otlp_test_event()])
        assert backend._get_worker() is None
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_sdk_flush_does_not_block_observing_caller() -> None:
    entered, release = threading.Event(), threading.Event()

    def flush(_timeout_millis: int) -> None:
        entered.set()
        assert release.wait(5)

    backend, logs, _ = _backend(logs=_Provider(on_flush=flush))
    backend.export_batch([otlp_test_event()])
    try:
        started = time.monotonic()
        assert backend.flush(timeout=0.01).status is DrainStatus.UNFINISHED
        assert entered.is_set()
        assert time.monotonic() - started < 1
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert backend._worker is not None
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set()


def test_full_queue_stop_uses_same_worker_and_preserves_admitted_records() -> None:
    entered, release = threading.Event(), threading.Event()
    delivered: list[int] = []

    def emit(record: Any) -> None:
        entered.set()
        assert release.wait(5)
        delivered.append(json.loads(record.body)["attributes"]["seq"])

    backend, logs, _ = _backend(logs=_Provider(on_emit=emit), queue_maxsize=1)
    first = otlp_test_event(attributes={"seq": 1})
    second = otlp_test_event(attributes={"seq": 2})
    after_stop = otlp_test_event(attributes={"seq": 3})
    backend.export_batch([first])
    try:
        assert entered.wait(5)
        backend.export_batch([second])
        assert backend._queue.full()
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        backend.export_batch([after_stop])
        assert not logs.closed.is_set()
    finally:
        release.set()
        assert backend._worker is not None
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert delivered == [1, 2]


def test_unknown_error_after_stop_is_immediate_and_same_owner_raises(
    loguru_records: LogRecords,
) -> None:
    entered, release = threading.Event(), threading.Event()
    error = BaseException("unknown late initialization defect")

    def get_meter() -> object:
        entered.set()
        assert release.wait(5)
        raise error

    backend, _, _ = _backend(metrics=_Provider(on_get_meter=get_meter))
    worker = backend._get_worker()
    assert worker is not None
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert error not in _logged_exceptions(loguru_records)
        release.set()
        assert worker.finished.wait(5)
        assert _logged_exceptions(loguru_records) == [error]
        with pytest.raises(BaseException) as raised:
            backend.shutdown()
        assert raised.value is error and backend._worker is worker
    finally:
        release.set()
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()


def test_failed_sdk_build_keeps_partial_resources_until_owned_cleanup() -> None:
    cleaning, release = threading.Event(), threading.Event()

    def close() -> None:
        cleaning.set()
        assert release.wait(5)

    def fail_meter() -> object:
        raise RuntimeError("named SDK construction failure")

    logs, metrics = _Provider(on_shutdown=close), _Provider(on_get_meter=fail_meter)
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True, enabled=lambda: True, providers=(logs, metrics)
    )
    try:
        assert backend._ensure() is False
        assert cleaning.wait(5)
        worker = backend._worker
        assert worker is not None and worker.logs is logs and worker.metrics is metrics
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not metrics.closed.is_set()
    finally:
        release.set()
        worker = backend._worker
        assert worker is not None and worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_shutdown_replays_deferred_metrics_before_logs_once() -> None:
    calls: list[tuple[str, int]] = []
    logs = _Provider(
        on_emit=lambda r: calls.append(("log", int(json.loads(r.body)["attributes"]["in_total"])))
    )
    metrics = _Provider(on_metric=lambda v: calls.append(("metric", int(v))))
    backend, _, _ = _backend(logs=logs, metrics=metrics)
    backend.defer_until_exit()
    backend.export_batch(
        [otlp_test_event(attributes={"in_total": 1}), otlp_test_event(attributes={"in_total": 2})]
    )
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert calls == [("metric", 1), ("metric", 2), ("log", 1), ("log", 2)]
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert len(calls) == 4


def test_disabled_nonempty_deferred_shutdown_does_not_start_sdk() -> None:
    reads: list[bool] = []
    delivered: list[Any] = []

    def enabled_at_bringup() -> bool:
        reads.append(False)
        return False

    backend, logs, metrics = _backend(
        logs=_Provider(on_emit=delivered.append), enabled=enabled_at_bringup
    )
    backend.defer_until_exit()
    backend.export_batch([otlp_test_event()])
    assert reads == []
    result = backend.shutdown()
    assert reads == [False] and backend._closed.is_set()
    assert backend._deferral.stop(timeout=5) and result.status is DrainStatus.UNFINISHED
    assert backend._worker is None and backend._queue.qsize() == 1 and delivered == []
    assert not logs.flushed.is_set() and not logs.closed.is_set() and not metrics.closed.is_set()


def test_enabled_nonempty_deferred_shutdown_reads_flag_only_at_bringup() -> None:
    reads: list[bool] = []
    delivered: list[int] = []

    def enabled_at_bringup() -> bool:
        reads.append(True)
        return True

    backend, logs, metrics = _backend(
        logs=_Provider(
            on_emit=lambda r: delivered.append(int(json.loads(r.body)["attributes"]["seq"]))
        ),
        enabled=enabled_at_bringup,
    )
    backend.defer_until_exit()
    backend.export_batch([otlp_test_event(attributes={"seq": 7})])
    assert reads == [] and backend.shutdown().status is DrainStatus.COMPLETED
    assert (
        reads == [True] and backend._worker is not None and not backend._worker._thread.is_alive()
    )
    assert delivered == [7] and logs.closed.is_set() and metrics.closed.is_set()


def test_primary_error_survives_blocked_secondary_cleanup(
    loguru_records: LogRecords,
) -> None:
    primary = BaseException("original writer defect")
    secondary = BaseException("secondary SDK teardown defect")
    cleaning, release = threading.Event(), threading.Event()

    def get_meter() -> object:
        raise primary

    def close() -> None:
        cleaning.set()
        assert release.wait(5)
        raise secondary

    backend, _logs, metrics = _backend(
        logs=_Provider(on_shutdown=close), metrics=_Provider(on_get_meter=get_meter)
    )
    worker = backend._get_worker()
    assert worker is not None
    try:
        assert cleaning.wait(5)
        assert _logged_exceptions(loguru_records)[0] is primary
        with pytest.raises(BaseException) as raised:
            backend.shutdown(timeout=0.01)
        assert raised.value is primary and worker._thread.is_alive()
        release.set()
        assert worker.finished.wait(5)
        assert secondary in _logged_exceptions(loguru_records) and metrics.closed.is_set()
        with pytest.raises(BaseException) as raised_again:
            backend.shutdown()
        assert raised_again.value is primary
    finally:
        release.set()
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()


def test_known_sdk_shutdown_failure_is_isolated_but_not_claimed_complete(
    loguru_records: LogRecords,
) -> None:
    def close() -> None:
        raise RuntimeError("named optional exporter cleanup failure")

    backend, _logs, metrics = _backend(logs=_Provider(on_shutdown=close))
    assert backend._ensure()
    assert backend.shutdown().status is DrainStatus.UNFINISHED
    assert backend._worker is not None
    assert backend._worker.finished.is_set()
    assert not backend._worker._thread.is_alive()
    assert any("otlp logs shutdown" in r["message"] for r in loguru_records)
    assert metrics.closed.is_set()
    assert backend.shutdown().status is DrainStatus.UNFINISHED


def test_deferral_completion_in_flight_cannot_close_providers_under_replay() -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[tuple[str, int]] = []

    def get_meter() -> object:
        entered.set()
        assert release.wait(5)
        return _Meter(lambda v: calls.append(("metric", int(v))))

    logs = _Provider(
        on_emit=lambda r: calls.append(("log", int(json.loads(r.body)["attributes"]["in_total"])))
    )
    metrics = _Provider(on_get_meter=get_meter)
    backend, _, _ = _backend(logs=logs, metrics=metrics)
    backend.defer_until_exit()
    event = otlp_test_event(attributes={"in_total": 5})
    backend.export_batch([event])
    caller = threading.Thread(target=backend.finalize)
    caller.start()
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        caller.join(timeout=5)
        assert not caller.is_alive()
        assert not logs.closed.is_set()
        release.set()
        assert backend._worker is not None
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
        assert calls == [("metric", 5), ("log", 5)]
    finally:
        release.set()
        caller.join(timeout=5)
        assert backend._worker is not None
        backend._worker._thread.join(timeout=5)
        assert not caller.is_alive() and not backend._worker._thread.is_alive()


def test_admitted_metric_call_retains_provider_until_it_finishes() -> None:
    entered, release = threading.Event(), threading.Event()
    metrics: _Provider

    def record_metric(_value: int | float) -> None:
        entered.set()
        assert release.wait(5)
        assert not metrics.closed.is_set()

    metrics = _Provider(on_metric=record_metric)
    backend, logs, _ = _backend(metrics=metrics)
    caller = threading.Thread(
        target=backend.export_batch,
        args=([otlp_test_event(attributes={"in_total": 1})],),
    )
    caller.start()
    try:
        assert entered.wait(5)
        assert backend.shutdown(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not logs.closed.is_set() and not metrics.closed.is_set()
        release.set()
        caller.join(timeout=5)
        assert backend._worker is not None
        assert backend._worker.finished.wait(5)
        assert backend.shutdown().status is DrainStatus.COMPLETED
        assert logs.closed.is_set() and metrics.closed.is_set()
    finally:
        release.set()
        caller.join(timeout=5)
        assert backend._worker is not None
        backend._worker._thread.join(timeout=5)
        assert not caller.is_alive() and not backend._worker._thread.is_alive()


def test_real_sdk_partial_build_threads_are_owned_and_closed(tmp_path: Path) -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OkProbeHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        port = server.server_address[1]
        backend = telemetry_otlp.OtlpBackend(
            export_allowed=lambda: True,
            enabled=lambda: True,
            endpoint=f"http://127.0.0.1:{port}",
            home=tmp_path / "missing-home",
        )
        assert backend._ensure() is False
        worker = backend._worker
        assert worker is not None
        assert worker.finished.wait(5)
        assert worker.logs is not None and worker.metric_reader is not None
        assert worker.metrics is None
        processor = worker.logs._multi_log_record_processor._log_record_processors[0]
        assert not processor._batch_processor._worker_thread.is_alive()
        assert not worker.metric_reader._daemon_thread.is_alive()
        assert backend.shutdown().status is DrainStatus.COMPLETED
    finally:
        server.shutdown()
        server.server_close()


def test_failed_teardown_owner_is_not_overwritten_at_retry_deadline() -> None:
    attempts: list[int] = []

    def close() -> None:
        raise RuntimeError("SDK cannot close the half-built resource")

    def fail_meter() -> object:
        attempts.append(len(attempts) + 1)
        raise RuntimeError("named SDK init failure")

    logs = _Provider(on_shutdown=close)
    metrics = _Provider(on_get_meter=fail_meter)
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        retry_interval_s=0.05,
        providers=(logs, metrics),
    )
    assert backend._ensure() is False
    worker = backend._worker
    assert worker is not None
    assert worker.finished.wait(5)
    assert backend._init_failed_at is not None
    time.sleep(0.06)
    assert backend._ensure() is False
    assert backend._worker is worker
    assert len(attempts) == 1
    assert backend.shutdown().status is DrainStatus.UNFINISHED


def test_unknown_failed_attempt_still_closes_deferred_admission_and_clock() -> None:
    error = BaseException("failed attempt before deferred stop")

    def fail_meter() -> object:
        raise error

    backend, _, _ = _backend(metrics=_Provider(on_get_meter=fail_meter))
    backend.defer_until_exit()
    backend.export_batch([otlp_test_event()])
    worker = backend._get_worker()
    assert worker is not None
    assert worker.finished.wait(5)
    try:
        with pytest.raises(BaseException) as raised:
            backend.shutdown(timeout=0.01)
        assert raised.value is error
        assert backend._closed.is_set()
        assert backend._deferral._clock is not None
        assert backend._deferral._clock.finished.is_set()
    finally:
        assert backend._deferral.stop(timeout=5)
        worker._thread.join(timeout=5)
        assert not worker._thread.is_alive()


def test_empty_exporting_pipeline_stops_without_reading_identity() -> None:
    def reject() -> bool:
        raise AssertionError("empty close must not read identity")

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=lambda _events, _exporter: None, export_allowed=reject
    )
    assert pipeline.stop(timeout=1).status is DrainStatus.COMPLETED
    assert pipeline.stopped
    assert pipeline._exporter._worker is None
    assert pipeline._exporter._export_decision is None


def test_stop_refuses_non_finite_and_negative_timeout() -> None:
    def reject() -> bool:
        raise AssertionError("a refused budget must not read identity")

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=lambda _events, _exporter: None, export_allowed=reject
    )
    for bad in (float("nan"), -1.0):
        with pytest.raises(ValueError, match="finite and non-negative"):
            pipeline.stop(timeout=bad)
        assert not pipeline.stopped
    assert pipeline.stop(timeout=1).status is DrainStatus.COMPLETED
    assert pipeline.stopped


def test_unfinished_ordinary_writer_keeps_its_exporter_alive() -> None:
    backend, logs, metrics = _backend()
    entered, release = threading.Event(), threading.Event()

    def write(_events: list[Event], _exporter: Any) -> None:
        entered.set()
        assert release.wait(5)

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=write, export_allowed=lambda: True, exporter=backend
    )
    assert backend._ensure()
    pipeline.enqueue(otlp_test_event())
    assert entered.wait(2)
    try:
        assert pipeline.stop(timeout=0.01).status is DrainStatus.UNFINISHED
        assert not pipeline.stopped
        assert not backend._closed.is_set()
        assert not logs.closed.is_set()
        assert not metrics.closed.is_set()
    finally:
        release.set()
        assert pipeline.stop(timeout=2).status is DrainStatus.COMPLETED
    assert pipeline.stopped
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_unfinished_exporter_keeps_pipeline_for_later_observation() -> None:
    entered, release = threading.Event(), threading.Event()

    def close() -> None:
        entered.set()
        assert release.wait(5)

    backend, logs, metrics = _backend(logs=_Provider(on_shutdown=close))
    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=lambda _events, _exporter: None, export_allowed=lambda: True, exporter=backend
    )
    assert backend._ensure()
    try:
        assert pipeline.stop(timeout=0.1).status is DrainStatus.UNFINISHED
        assert entered.wait(2)
        assert not pipeline.stopped
        assert backend._worker is not None
    finally:
        release.set()
        assert pipeline.stop(timeout=2).status is DrainStatus.COMPLETED
    assert pipeline.stopped
    assert logs.closed.is_set() and metrics.closed.is_set()


def test_two_stage_stop_hands_stage_two_the_real_remaining_time() -> None:
    before = {thread.ident for thread in threading.enumerate()}
    logs, metrics = _BlockingProvider(), _Provider()
    writer_started, writer_gate = threading.Event(), threading.Event()
    writer_done: list[float] = []
    stop_timeout = 0.6
    writer_sleep = 0.3

    def write(_events: list[Event], _exporter: Any) -> None:
        writer_started.set()
        assert writer_gate.wait(5)
        time.sleep(writer_sleep)
        writer_done.append(time.monotonic())

    backend, _, _ = _backend(logs=logs, metrics=metrics)
    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=write, export_allowed=lambda: True, exporter=backend
    )
    assert backend._ensure()
    pipeline.enqueue(otlp_test_event())
    assert writer_started.wait(5)
    started = time.monotonic()
    writer_gate.set()
    result = pipeline.stop(timeout=stop_timeout)
    elapsed = time.monotonic() - started
    assert result.status is DrainStatus.UNFINISHED
    assert result.phase is DrainPhase.STOP
    assert not pipeline.stopped
    assert writer_done and writer_done[0] - started >= writer_sleep * 0.8
    assert logs.teardown_entered.wait(2)
    assert logs.teardown_entered_at >= writer_done[0]
    assert elapsed <= stop_timeout + 0.15
    _assert_release_completes(pipeline, logs.release)
    assert backend._worker is not None
    assert {thread.ident for thread in threading.enumerate()} <= before


def test_zero_timeout_stage_one_leaves_the_exporter_untouched() -> None:
    writer_started, release_writer = threading.Event(), threading.Event()

    def write(_events: list[Event], _exporter: Any) -> None:
        writer_started.set()
        assert release_writer.wait(5)

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=write, export_allowed=lambda: True
    )
    backend = pipeline._exporter
    pipeline.enqueue(otlp_test_event())
    assert writer_started.wait(5)
    started = time.monotonic()
    result = pipeline.stop(timeout=0)
    assert time.monotonic() - started < 0.5
    assert result.status is DrainStatus.UNFINISHED
    assert result.phase is DrainPhase.STOP
    assert not pipeline.stopped
    assert backend._worker is None
    assert not backend._closed.is_set()
    _assert_release_completes(pipeline, release_writer)


def test_blocked_writer_and_blocked_exporter_release_in_stages() -> None:
    logs, metrics = _BlockingProvider(), _Provider()
    writer_started, release_writer = threading.Event(), threading.Event()

    def write(_events: list[Event], _exporter: Any) -> None:
        writer_started.set()
        assert release_writer.wait(5)

    backend, _, _ = _backend(logs=logs, metrics=metrics)
    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=write, export_allowed=lambda: True, exporter=backend
    )
    assert backend._ensure()
    pipeline.enqueue(otlp_test_event())
    assert writer_started.wait(5)
    assert pipeline.stop(timeout=0.3).status is DrainStatus.UNFINISHED
    assert not backend._closed.is_set()
    assert not logs.teardown_entered.is_set()
    release_writer.set()
    assert pipeline.stop(timeout=0.3).status is DrainStatus.UNFINISHED
    assert logs.teardown_entered.wait(2)
    assert not pipeline.stopped
    _assert_release_completes(pipeline, logs.release)
    assert backend._worker is not None
    assert not backend._worker._thread.is_alive()


def test_stop_from_the_writer_thread_is_refused() -> None:
    pipeline: telemetry_otlp_exporting.ExportingEventPipeline

    def write(_events: list[Event], _exporter: Any) -> None:
        with pytest.raises(RuntimeError) as raised:
            pipeline.stop(timeout=0)
        assert "event drain cannot join itself" in str(raised.value)

    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=write, export_allowed=lambda: True
    )
    pipeline.enqueue(otlp_test_event())
    assert pipeline.stop(timeout=2).status is DrainStatus.COMPLETED
    assert pipeline.stopped


def test_concurrent_stop_calls_share_the_blocked_observation() -> None:
    logs, metrics = _BlockingProvider(), _Provider()
    backend, _, _ = _backend(logs=logs, metrics=metrics)
    pipeline = telemetry_otlp_exporting.ExportingEventPipeline(
        writer=lambda _events, _exporter: None, export_allowed=lambda: True, exporter=backend
    )
    assert backend._ensure()
    results: list[DrainResult] = []
    failures: list[BaseException] = []

    def call_stop() -> None:
        try:
            results.append(pipeline.stop(timeout=0.3))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=call_stop) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not failures
    assert len(results) == 2
    assert all(result.status is DrainStatus.UNFINISHED for result in results)
    assert pipeline.stop(timeout=0.05).status is DrainStatus.UNFINISHED
    assert logs.teardown_entered.wait(2)
    assert not pipeline.stopped
    _assert_release_completes(pipeline, logs.release)
    assert backend._worker is not None
    assert not backend._worker._thread.is_alive()


def test_close_time_gate_fault_raises_after_the_finite_observation() -> None:
    logs, metrics = _BlockingProvider(), _Provider()
    fail_on_close = False

    def enabled_gate() -> bool:
        if fail_on_close:
            raise RuntimeError("close-time eligibility read failed")
        return True

    backend, _, _ = _backend(logs=logs, metrics=metrics, enabled=enabled_gate)
    backend.defer_until_exit()
    backend.export_batch([otlp_test_event()])
    assert backend._deferral.is_active()
    assert backend._ensure()
    fail_on_close = True
    started = time.monotonic()
    with pytest.raises(RuntimeError) as raised:
        backend.shutdown(timeout=0.3)
    assert "close-time eligibility read failed" in str(raised.value)
    assert time.monotonic() - started >= 0.2
    assert logs.teardown_entered.wait(2)
    assert backend._worker is not None
    assert backend._worker._thread.is_alive()
    logs.release.set()
    fail_on_close = False
    assert backend.shutdown().status is DrainStatus.COMPLETED
    assert backend._worker is not None
    assert not backend._worker._thread.is_alive()
