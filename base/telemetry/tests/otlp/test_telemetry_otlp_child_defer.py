"""Deferred OTLP export for exec children (task #3816 M4b).

Pins the deferral state machine (`base.telemetry.otlp.telemetry_otlp_defer` + the backend
seams): hold without bring-up, the flush guard, saturation and max-age
transitions, metrics replay exactly once, empty-hold completion, and the env
knobs' parity with the declared Settings fields.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from base.telemetry.delivery.defer_plan import (
    CHILD_DEFER_MAX_AGE_DEFAULT_S,
    ChildDeferPlan,
    child_defer_plan,
    parse_defer_flag,
    parse_defer_seconds,
)
from base.telemetry.delivery.receipts import Event
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.otlp.test_telemetry_otlp import CHILD_DEFER

_AGENT = 8902


def _event(**overrides: Any) -> Event:
    fields: dict[str, Any] = {
        "ts": datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC),
        "trace_id": "abcd" * 8,
        "span_id": "ef01" * 4,
        "agent_id": _AGENT,
        "machine": "test-mac",
        "cluster": ".meiva-test",
        "process": "test-proc",
        "category": "telemetry",
        "event_name": "sdk_call",
        "level": "info",
        "source": "test",
        "target_agent_id": None,
        "attributes": {},
    }
    fields.update(overrides)
    return Event(**fields)


def _make_backend(*, mirror_dir: Path, queue_maxsize: int = 8) -> tuple[Any, Any, Any]:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import (
        InMemoryLogRecordExporter,
        SimpleLogRecordProcessor,
    )
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    log_exporter = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    resource_exporter: Any = telemetry_otlp.EventDimensionResourceExporter(log_exporter)
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(resource_exporter))
    metric_reader = InMemoryMetricReader()
    metric_provider = MeterProvider(metric_readers=[metric_reader])
    backend = telemetry_otlp.OtlpBackend(
        export_allowed=lambda: True,
        enabled=lambda: True,
        providers=(logger_provider, metric_provider),
        queue_maxsize=queue_maxsize,
        mirror_dir=mirror_dir,
    )
    return backend, log_exporter, metric_reader


def _metrics(metric_reader: Any) -> dict[str, Any]:
    data = metric_reader.get_metrics_data()
    out: dict[str, Any] = {}
    if data is None:
        return out
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                out[m.name] = m
    return out


@pytest.fixture
def otlp_backend(tmp_path: Path) -> Any:
    """An `OtlpBackend` with in-memory providers, retained by this test
    owner (so module-level wrappers hit it); not yet armed."""
    backend, log_exporter, metric_reader = _make_backend(mirror_dir=tmp_path)

    yield backend, log_exporter, metric_reader
    backend.shutdown()


@pytest.fixture
def deferred_backend(otlp_backend: Any) -> Any:
    backend, log_exporter, metric_reader = otlp_backend
    backend.defer_until_exit(CHILD_DEFER)
    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    return backend, log_exporter, metric_reader


def test_hold_without_bringup(deferred_backend: Any) -> None:
    """Held batches touch neither the settings chain nor the OTel stack."""
    backend, log_exporter, _ = deferred_backend

    for i in range(3):
        backend.export_batch([_event(attributes={"n": i})])

    assert backend._logs is None  # pyright: ignore[reportUnknownMemberType]
    assert backend._worker is None
    assert backend._export_decision is None
    assert backend._queue.qsize() == 3  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_flush_while_deferred_keeps_hold(deferred_backend: Any) -> None:
    """The flush guard: a deferred flush must not drain (and thus drop) the hold."""
    backend, log_exporter, _ = deferred_backend
    backend.export_batch([_event(), _event()])

    backend.flush()

    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert backend._queue.qsize() == 2  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_finalize_completes_hold_and_replays_metrics_once(deferred_backend: Any) -> None:
    backend, log_exporter, metric_reader = deferred_backend
    backend.export_batch([_event(attributes={"tokens": 5})])
    backend.export_batch([_event(attributes={"tokens": 7})])

    backend.finalize()

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    logs = log_exporter.get_finished_logs()
    assert len(logs) == 2
    metrics = _metrics(metric_reader)
    name = next(n for n in metrics if n.endswith("tokens"))
    assert sum(dp.value for dp in metrics[name].data.data_points) == 12
    # Flushed exactly-once after completion: a second finalize is a no-op.
    backend.finalize()
    assert len(log_exporter.get_finished_logs()) == 2


def test_saturation_goes_live_once(tmp_path: Path) -> None:
    backend, log_exporter, _ = _make_backend(queue_maxsize=4, mirror_dir=tmp_path)
    try:
        backend.defer_until_exit(CHILD_DEFER)
        backend.export_batch([_event() for _ in range(4)])
        assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
        assert backend._worker is None
        backend.export_batch([_event()])  # saturates -> bring-up -> live

        assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
        assert backend._worker is not None
        backend.flush()
        assert len(log_exporter.get_finished_logs()) >= 4
    finally:
        backend.shutdown()


def test_max_age_clock_goes_live(otlp_backend: Any) -> None:
    assert parse_defer_seconds("0.15", default=30.0) == 0.15
    backend, log_exporter, _ = otlp_backend
    backend.defer_until_exit(ChildDeferPlan(enabled=True, max_age_s=0.15))
    backend.export_batch([_event()])

    deadline = time.monotonic() + 5.0
    while backend._deferral.is_active() and time.monotonic() < deadline:  # pyright: ignore[reportUnknownMemberType]
        time.sleep(0.02)

    assert not backend._deferral.is_active(), "max-age clock must complete the deferral"  # pyright: ignore[reportUnknownMemberType]
    assert backend._worker is not None
    backend.flush()
    assert len(log_exporter.get_finished_logs()) == 1


def test_empty_hold_finalize_skips_bringup(deferred_backend: Any) -> None:
    """A deferred child whose records never arrived completes for free."""
    backend, _, _ = deferred_backend

    backend.finalize()

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert backend._worker is None
    assert backend._thread is None  # pyright: ignore[reportUnknownMemberType]


def test_explicit_backend_arms_and_completes(otlp_backend: Any) -> None:
    backend, log_exporter, _ = otlp_backend

    backend.defer_until_exit(CHILD_DEFER)
    assert backend.deferred_state() is True
    backend.export_batch([_event()])
    assert backend._queue.qsize() == 1  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()

    backend.finalize()
    assert backend.deferred_state() is False
    assert len(log_exporter.get_finished_logs()) == 1


def test_flag_off_disables_defer(otlp_backend: Any) -> None:
    """MEIVA_TELEMETRY_OTLP_CHILD_DEFER=0 restores the eager first-record bring-up."""
    assert parse_defer_flag("0", default=True) is False
    backend, log_exporter, _ = otlp_backend

    backend.defer_until_exit(ChildDeferPlan(enabled=False, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S))

    assert not backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    backend.export_batch([_event()])
    backend.flush()
    assert len(log_exporter.get_finished_logs()) == 1


# meiva:note
# The plan a child builds from its raw environment mirrors the declared Settings defaults
# (one home per fact — the child cannot import the config module).
def test_env_defaults_locked_to_settings_fields() -> None:
    from base.config import settings

    fields = type(settings.observability).model_fields
    assert fields["telemetry_otlp_child_defer"].default is True
    assert fields["telemetry_otlp_child_defer"].alias == "MEIVA_TELEMETRY_OTLP_CHILD_DEFER"
    assert fields["telemetry_otlp_child_defer_max_age_s"].default == CHILD_DEFER_MAX_AGE_DEFAULT_S
    assert (
        fields["telemetry_otlp_child_defer_max_age_s"].alias
        == "MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S"
    )


def test_ensure_refuses_during_finalization(otlp_backend: Any) -> None:
    """A first SDK construction during interpreter shutdown is refused.

    The old eager warmup() existed to keep construction off the shutdown path;
    exec children now defer instead, and `_ensure()` enforces the invariant at
    the single construction point — the record path degrades to unsent (the
    JSONL mirror retains it)."""
    backend, log_exporter, _ = otlp_backend

    assert backend._ensure(finalizing=True) is False
    assert backend._logs is None  # pyright: ignore[reportUnknownMemberType]
    assert backend._thread is None  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_deferred_completion_during_finalization_keeps_hold(deferred_backend: Any) -> None:
    """The deferral's shutdown fallback must not construct during finalization:
    the hold stays intact instead (JSONL-only), never a shutdown-time bring-up."""
    backend, log_exporter, _ = deferred_backend
    backend.export_batch([_event()])

    backend.finalize(finalizing=True)

    assert backend._deferral.is_active()  # pyright: ignore[reportUnknownMemberType]
    assert backend._logs is None  # pyright: ignore[reportUnknownMemberType]
    assert not log_exporter.get_finished_logs()


def test_invalid_eligibility_preserves_deferred_records_and_propagates(tmp_path: Path) -> None:
    from base.cluster.machine import MachineRoleInvalid

    error = MachineRoleInvalid("invalid configured role")

    def invalid() -> bool:
        raise error

    backend = telemetry_otlp.OtlpBackend(
        export_allowed=invalid, enabled=lambda: True, mirror_dir=tmp_path
    )
    backend.defer_until_exit(CHILD_DEFER)
    backend.export_batch([_event()])
    try:
        with pytest.raises(MachineRoleInvalid) as raised:
            backend.finalize()
        assert raised.value is error
        assert backend._queue.qsize() == 1
        assert backend._worker is None
    finally:
        with pytest.raises(MachineRoleInvalid):
            backend.shutdown()


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, ChildDeferPlan(enabled=True, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S)),
        (
            {"MEIVA_TELEMETRY_OTLP_CHILD_DEFER": "off"},
            ChildDeferPlan(enabled=False, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S),
        ),
        (
            {"MEIVA_TELEMETRY_OTLP_CHILD_DEFER": "garbage"},
            ChildDeferPlan(enabled=True, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S),
        ),
        (
            {"MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S": "0.15"},
            ChildDeferPlan(enabled=True, max_age_s=0.15),
        ),
        (
            {"MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S": "0"},
            ChildDeferPlan(enabled=True, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S),
        ),
        (
            {"MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S": "nan"},
            ChildDeferPlan(enabled=True, max_age_s=CHILD_DEFER_MAX_AGE_DEFAULT_S),
        ),
    ],
)
def test_child_defer_plan_is_read_from_the_environment_it_is_given(
    environ: dict[str, str], expected: ChildDeferPlan
) -> None:
    assert child_defer_plan(environ) == expected


@pytest.mark.parametrize("max_age_s", [0.0, -1.0, float("inf"), float("nan")])
def test_child_defer_plan_rejects_an_unusable_max_age(max_age_s: float) -> None:
    with pytest.raises(ValueError, match="max age"):
        ChildDeferPlan(enabled=True, max_age_s=max_age_s)
