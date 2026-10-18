from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from base.daemon.health import DaemonProbe
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root import health as health_mod
from services.supervision.meiva_root.failure_state import (
    FailureKind,
    UnitFailureFacts,
    derive_kind,
    describe,
)
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor
from services.supervision.meiva_root.intent_store import RestartFailure, RestartStage
from services.supervision.meiva_root.probes import ProbeRegistry


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    @property
    def calls(self) -> list[dict[str, object]]:
        return [
            {"message": r["message"], "level": r["level"].name.lower(), **r["extra"]}
            for r in self._records
        ]

    def events(self, name: str) -> list[dict[str, object]]:
        return [c for c in self.calls if c.get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


def _failure(stage: RestartStage = RestartStage.UP, detail: str = "boom") -> RestartFailure:
    return RestartFailure(stage=stage, since=12.0, detail=detail)


def test_derive_kind_priority_and_the_intent_gate() -> None:
    facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    assert derive_kind(facts, restart_limit_reached=False) is None
    assert derive_kind(facts, restart_limit_reached=True) is FailureKind.BREAKER_OPEN
    failed = UnitFailureFacts(intent_running=True, restart_failed=_failure())
    assert derive_kind(failed, restart_limit_reached=False) is FailureKind.RESTART_FAILED
    assert derive_kind(failed, restart_limit_reached=True) is FailureKind.BREAKER_OPEN
    stopped = UnitFailureFacts(intent_running=False, restart_failed=_failure())
    assert derive_kind(stopped, restart_limit_reached=True) is None


def test_describe_carries_the_kind_evidence() -> None:
    failed = UnitFailureFacts(
        intent_running=True, restart_failed=_failure(RestartStage.DOWN, "stop refused")
    )
    assert describe(failed, FailureKind.RESTART_FAILED) == (
        "replacement failed at its down half: stop refused"
    )
    facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    assert describe(facts, FailureKind.BREAKER_OPEN, "no probe") == "no probe"
    assert describe(facts, FailureKind.BREAKER_OPEN) == (
        "automatic restart attempts exhausted; circuit breaker open"
    )
    assert describe(failed, FailureKind.BREAKER_OPEN) == (
        "replacement failed at its down half: stop refused; circuit breaker open"
    )


class _Supervisor:
    def __init__(self) -> None:
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.facts = UnitFailureFacts(intent_running=True, restart_failed=None)
        self.deferrals: dict[str, str] = {}

    async def restart(self, unit_id: str) -> dict[str, object]:
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float]:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return self.deferrals.get(unit_id)

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        return self.facts


class _Cell:
    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict
        self.calls = 0

    def __call__(self) -> DaemonProbe:
        self.calls += 1
        return self.verdict


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 0.02
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(health_mod, "monotonic", fake)
    return fake


def _monitor(supervisor: _Supervisor, probe: Callable[[], DaemonProbe]) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "max_restart_attempts": 3,
        "restart_backoff_s": (0.0,),
    }
    return HealthMonitor(supervisor, registry, config=HealthConfig(**values))


async def test_exhausted_attempts_are_reported_every_round_then_clear_on_alive(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.down("no"))
    monitor = _monitor(_Supervisor(), probe)
    await monitor.run_round()
    await monitor.run_round()
    assert recorder.events("root_unit_failure_state") == []
    await monitor.run_round()
    await monitor.run_round()
    states = recorder.events("root_unit_failure_state")
    assert [(s["unit"], s["kind"], s["level"]) for s in states] == [
        ("svc", "breaker_open", "warning"),
        ("svc", "breaker_open", "warning"),
    ]
    probe.verdict = DaemonProbe.up("ok")
    await monitor.run_round()
    assert len(recorder.events("root_unit_failure_state")) == 2


async def test_recorded_restart_failure_is_a_state_signal(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")))
    supervisor.facts = UnitFailureFacts(
        intent_running=True,
        restart_failed=_failure(RestartStage.DOWN, "stop refused"),
    )
    await monitor.run_round()
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    await monitor.run_round()
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    await monitor.run_round()
    states = recorder.events("root_unit_failure_state")
    assert [s["kind"] for s in states] == ["restart_failed", "restart_failed"]
    assert states[0]["detail"] == "replacement failed at its down half: stop refused"


async def test_terminal_verdict_is_reported_every_round_at_warning(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.port_taken("alien daemon"))
    monitor = _monitor(_Supervisor(), probe)
    await monitor.run_round()
    await monitor.run_round()
    reports = recorder.events("root_unit_not_revivable")
    assert [(r["unit"], r["detail"], r["level"]) for r in reports] == [
        ("svc", "alien daemon", "warning"),
        ("svc", "alien daemon", "warning"),
    ]
    probe.verdict = DaemonProbe.up("healthy again")
    await monitor.run_round()
    assert len(recorder.events("root_unit_not_revivable")) == 2


# meiva:note
# The restart ladder and the dependency-recovery grant are the other half of a unit's
# failure signals: what still replaces it once its breaker is open, and which rounds
# are allowed to consume that permission (task #5315).
def _paced_monitor(
    supervisor: _Supervisor,
    probe: Callable[[], DaemonProbe],
    *,
    database: Callable[[], DaemonProbe] | None = None,
    startup_grace_s: float = 0.0,
) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    return HealthMonitor(
        supervisor,
        registry,
        config=HealthConfig(
            interval_s=0.01,
            startup_grace_s=startup_grace_s,
            max_restart_attempts=3,
            restart_backoff_s=(60.0, 300.0, 900.0),
        ),
        db_recovery_probe=database,
    )


async def _spend_the_budget(monitor: HealthMonitor, clock: _Clock) -> None:
    for wait in (0, 60, 300):
        clock.now += wait
        await monitor.run_round()
    assert monitor.snapshot()["svc"].breaker_open


def _raising() -> DaemonProbe:
    raise RuntimeError("gate refused")


async def test_a_recovery_grant_survives_a_startup_grace(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    supervisor = _Supervisor()
    monitor = _paced_monitor(
        supervisor, _Cell(DaemonProbe.down("no")), database=database, startup_grace_s=180
    )
    await _spend_the_budget(monitor, clock)
    supervisor.generation = (OwnedProcess(43, 101.0, None), clock.now)
    database.verdict = DaemonProbe.up("database answered")
    clock.now += 60
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    clock.now += 181
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_a_recovery_grant_survives_a_terminal_observation(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    unit = _Cell(DaemonProbe.down("no"))
    monitor = _paced_monitor(_Supervisor(), unit, database=database)
    await _spend_the_budget(monitor, clock)
    unit.verdict = DaemonProbe.port_taken("alien daemon")
    database.verdict = DaemonProbe.up("database answered")
    clock.now += 60
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    unit.verdict = DaemonProbe.down("no")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_the_first_database_observation_grants_exactly_one_attempt(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.up("database answered"))
    monitor = _paced_monitor(_Supervisor(), _Cell(DaemonProbe.down("no")), database=database)
    await _spend_the_budget(monitor, clock)
    clock.now += 60
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_a_database_that_fails_again_withdraws_the_grant(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    unit = _Cell(DaemonProbe.down("no"))
    monitor = _paced_monitor(_Supervisor(), unit, database=database)
    await _spend_the_budget(monitor, clock)
    unit.verdict = DaemonProbe.unavailable("inspection failed")
    database.verdict = DaemonProbe.up("database answered")
    clock.now += 60
    await monitor.run_round()
    database.verdict = DaemonProbe.down("database unreachable again")
    await monitor.run_round()
    unit.verdict = DaemonProbe.down("no")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3


async def test_a_raising_database_probe_never_releases_the_breaker(clock: _Clock) -> None:
    monitor = _paced_monitor(_Supervisor(), _Cell(DaemonProbe.down("no")), database=_raising)
    await _spend_the_budget(monitor, clock)
    for _ in range(3):
        clock.now += 60
        await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    assert monitor.snapshot()["svc"].breaker_open


async def test_a_held_down_unit_stops_the_database_dial(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    supervisor = _Supervisor()
    monitor = _paced_monitor(supervisor, _Cell(DaemonProbe.down("no")), database=database)
    await _spend_the_budget(monitor, clock)
    supervisor.deferrals["svc"] = "held down"
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    dials = database.calls
    await monitor.run_round()
    assert database.calls == dials
