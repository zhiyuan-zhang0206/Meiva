from pathlib import Path

import pytest

from base.cluster import derive
from base.cluster.machine import MachineNameMissing, MachineRoleInvalid
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.faults import InvalidRoleIdentity
from base.config import ConfigBoot
from base.paths.tests.homes import point_home
from base.telemetry import observability
from base.telemetry.otlp import telemetry_otlp
from base.telemetry.tests.otlp.identity_inputs import machine_identity


@pytest.mark.parametrize(
    ("name", "cluster", "expected"),
    [("runner", ".meiva", True), ("runner", "other", False), ("", ".meiva", False)],
)
def test_production_identity_requires_name_and_production_home(
    tmp_path: Path, name: str, cluster: str, expected: bool
) -> None:
    home = tmp_path / cluster
    home.mkdir()
    assert derive.production_identity(machine_identity(name=name), home=home) is expected


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
    tmp_path: Path,
    roles: frozenset[str] | None,
    marker: bool,
    override: bool,
    expected: bool,
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    if marker:
        (home / "lgtm-host").touch()
    assert (
        telemetry_otlp.observability_export_allowed(
            machine_identity(roles=roles), home=home, endpoint_explicit=override
        )
        is expected
    )


def test_explicit_endpoint_does_not_read_machine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    facts = machine_identity()
    bad_home = tmp_path / "bad_utf8"
    bad_home.mkdir()
    (bad_home / ".env").write_bytes(b"\xff")
    point_home(monkeypatch, bad_home)
    with pytest.raises(UnicodeDecodeError):
        ConfiguredMachineIdentity(ConfigBoot()).name()
    machine = ConfiguredMachineIdentity(ConfigBoot())
    machine.set(role=facts.role(), host=facts.host(), description=facts.description())
    assert telemetry_otlp.observability_export_allowed(
        machine, home=tmp_path, endpoint_explicit=True
    )
    with pytest.raises(UnicodeDecodeError):
        machine.name()


def test_missing_role_preserves_runner_bootstrap_gate(tmp_path: Path) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    machine = machine_identity(roles=None)
    assert telemetry_otlp.observability_export_allowed(machine, home=home, endpoint_explicit=False)


@pytest.mark.parametrize("operation", ["gateway", "station", "export"])
def test_invalid_roles_propagate_outside_transport_isolation(
    tmp_path: Path, operation: str
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    machine = InvalidRoleIdentity(machine_identity())
    with pytest.raises(MachineRoleInvalid, match="fixed test fault"):
        if operation == "gateway":
            observability.gateway_observability_home(machine, home=home)
        elif operation == "station":
            observability.home_is_observability_station(home, machine=machine, unit_home=home)
        else:
            telemetry_otlp.observability_export_allowed(machine, home=home, endpoint_explicit=False)


def test_each_backend_freezes_only_its_own_eligibility() -> None:
    decision = False
    calls: list[bool] = []

    def read() -> bool:
        calls.append(decision)
        return decision

    first = telemetry_otlp.OtlpBackend(export_allowed=read, enabled=lambda: True)
    assert first._enabled() is False
    decision = True
    assert first._enabled() is False
    second = telemetry_otlp.OtlpBackend(export_allowed=read, enabled=lambda: True)
    assert second._enabled() is True
    assert calls == [False, True]


@pytest.mark.parametrize("error", [MachineRoleInvalid, RuntimeError])
def test_backend_does_not_cache_identity_defects_as_disabled(
    error: type[Exception],
) -> None:
    def invalid() -> bool:
        raise error("bad identity")

    backend = telemetry_otlp.OtlpBackend(export_allowed=invalid, enabled=lambda: True)
    with pytest.raises(error, match="bad identity"):
        backend.warmup()
    assert backend._export_decision is None
    assert backend._worker is None


def test_disabled_backend_does_not_read_identity() -> None:
    def reject() -> bool:
        raise MachineNameMissing("identity remains cold")

    assert (
        telemetry_otlp.OtlpBackend(export_allowed=reject, enabled=lambda: False)._enabled() is False
    )


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
