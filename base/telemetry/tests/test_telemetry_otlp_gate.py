import os
from pathlib import Path

import pytest

from base import config
from base.cluster import derive
from base.cluster.machine import MachineNameMissing, MachineRoleInvalid, MachineRoleMissing
from base.telemetry import observability
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.otlp.identity_inputs import machine_identity


def _production_label(_home: Path | None = None) -> str:
    return ".meiva"


@pytest.mark.parametrize(
    ("name", "cluster", "expected"),
    [("runner", ".meiva", True), ("runner", "other", False), ("", ".meiva", False)],
)
def test_production_identity_requires_name_and_production_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, cluster: str, expected: bool
) -> None:
    def label(_home: Path) -> str:
        return cluster

    monkeypatch.setattr(derive, "home_label", label)
    assert derive.production_identity(machine_identity(name=name), home=tmp_path) is expected


@pytest.mark.parametrize(
    ("roles", "marker", "override", "expected"),
    [
        (frozenset({"agent-runner"}), False, False, True),
        (frozenset({"gateway"}), False, False, False),
        (frozenset({"gateway"}), True, False, True),
        (frozenset({"gateway"}), False, True, True),
        (frozenset({"gateway", "observability-station"}), False, False, True),
        (None, False, False, True),
    ],
)
def test_export_gate_roles_marker_and_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    roles: frozenset[str] | None,
    marker: bool,
    override: bool,
    expected: bool,
) -> None:
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    if override:
        monkeypatch.setitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", "http://explicit:4318")
    if marker:
        (tmp_path / "lgtm-host").touch()
    assert (
        telemetry_otlp.observability_export_allowed(machine_identity(roles=roles), home=tmp_path)
        is expected
    )


def test_explicit_endpoint_does_not_read_machine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    machine = machine_identity()

    def reject() -> str:
        raise AssertionError("explicit endpoints need no production identity")

    monkeypatch.setattr(machine, "name", reject)
    monkeypatch.setitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", "http://explicit:4318")
    assert telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


def test_missing_role_preserves_runner_bootstrap_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    machine = machine_identity()

    def missing() -> frozenset[str]:
        raise MachineRoleMissing("not configured")

    monkeypatch.setattr(machine, "role", missing)
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    assert telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


@pytest.mark.parametrize("operation", ["gateway", "station", "export"])
def test_invalid_roles_propagate_outside_transport_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str
) -> None:
    machine = machine_identity(roles=frozenset({"unknown"}))
    monkeypatch.setattr(derive, "home_label", _production_label)
    monkeypatch.delitem(os.environ, "MEIVA_TELEMETRY_OTLP_ENDPOINT", raising=False)
    with pytest.raises(MachineRoleInvalid):
        if operation == "gateway":
            observability.gateway_observability_home(machine, home=tmp_path)
        elif operation == "station":
            observability.home_is_observability_station(
                tmp_path, machine=machine, unit_home=tmp_path
            )
        else:
            telemetry_otlp.observability_export_allowed(machine, home=tmp_path)


def test_each_backend_freezes_only_its_own_eligibility(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.observability, "telemetry_otlp_enabled", True)
    decision = False
    calls: list[bool] = []

    def read() -> bool:
        calls.append(decision)
        return decision

    first = telemetry_otlp.OtlpBackend(export_allowed=read)
    assert first._enabled() is False
    decision = True
    assert first._enabled() is False
    second = telemetry_otlp.OtlpBackend(export_allowed=read)
    assert second._enabled() is True
    assert calls == [False, True]


@pytest.mark.parametrize("error", [MachineRoleInvalid, RuntimeError])
def test_backend_does_not_cache_identity_defects_as_disabled(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    monkeypatch.setattr(config.settings.observability, "telemetry_otlp_enabled", True)

    def invalid() -> bool:
        raise error("bad identity")

    backend = telemetry_otlp.OtlpBackend(export_allowed=invalid)
    with pytest.raises(error, match="bad identity"):
        backend.warmup()
    assert backend._export_decision is None
    assert backend._worker is None


def test_disabled_backend_does_not_read_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings.observability, "telemetry_otlp_enabled", False)

    def reject() -> bool:
        raise MachineNameMissing("identity remains cold")

    assert telemetry_otlp.OtlpBackend(export_allowed=reject)._enabled() is False


# meiva:note
# The exit seam must not start an exporter it never started: by then the SDK's executors
# are already shut down, so the attempt fails and reports itself through a pipeline that is
# closing — a warning plus traceback in a watcher's log for a process whose work finished
# (#5578). `stop()` therefore marks the backend closing *before* its own drain, which is
# where that bring-up used to happen.
# meiva:link base/telemetry/otlp/docs/telemetry-otlp/exit-flush.meiva.okf.md
def test_stop_marks_the_exporter_closing_before_the_drain() -> None:
    from datetime import UTC, datetime

    from base.telemetry.delivery.receipts import Event
    from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline

    seen: list[bool] = []
    pipeline = ExportingEventPipeline(
        writer=lambda _events, exporter: seen.append(exporter._closing),
        export_allowed=lambda: True,
    )
    pipeline.enqueue(
        Event(
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
            attributes={},
        )
    )
    pipeline.stop(timeout=5.0)

    assert seen == [True]
