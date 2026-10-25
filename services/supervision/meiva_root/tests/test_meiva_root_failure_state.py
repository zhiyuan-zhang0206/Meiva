from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from base.daemon.health import DaemonProbe
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root.failure_state import (
    FailureKind,
    UnitFailureFacts,
    derive_kind,
    describe,
)
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor, monotonic
from services.supervision.meiva_root.intent_store import IntentSource, RestartFailure, RestartStage
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
        self.restart_calls: list[str] = []

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
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
def clock() -> _Clock:
    return _Clock()


def _monitor(
    supervisor: _Supervisor,
    probe: Callable[[], DaemonProbe],
    *,
    clock: Callable[[], float] = monotonic,
) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "max_restart_attempts": 3,
        "restart_backoff_s": (0.0,),
    }
    return HealthMonitor(supervisor, registry, config=HealthConfig(**values), clock=clock)


async def test_exhausted_attempts_are_reported_every_round_then_clear_on_alive(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.down("no"))
    monitor = _monitor(_Supervisor(), probe, clock=clock)
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


# meiva:note
# #5525 family A: three automatic restarts recount one anonymous blocker unless the detail
# names the process answering the port. The probe carries it; the per-round failure-state
# signal must hand it to an operator unchanged, or the event is still "not this unit's daemon".
async def test_the_failure_state_signal_carries_the_named_port_holder(
    clock: _Clock, recorder: _Recorder
) -> None:
    holder = "listening: pid 4242 node (/usr/local/bin/node)"
    probe = _Cell(
        DaemonProbe.down(
            "http://127.0.0.1:6031/healthz answers but no pidfile at /run/x.pid — "
            f"not this unit's daemon; {holder}"
        )
    )
    monitor = _monitor(_Supervisor(), probe, clock=clock)
    for _ in range(4):
        await monitor.run_round()
    state = recorder.events("root_unit_failure_state")[-1]
    assert state["kind"] == "breaker_open"
    assert holder in str(state["detail"])


async def test_recorded_restart_failure_is_a_state_signal(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")), clock=clock)
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
    monitor = _monitor(_Supervisor(), probe, clock=clock)
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
    clock: Callable[[], float] = monotonic,
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
        clock=clock,
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
        supervisor,
        _Cell(DaemonProbe.down("no")),
        database=database,
        startup_grace_s=180,
        clock=clock,
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
    monitor = _paced_monitor(_Supervisor(), unit, database=database, clock=clock)
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
    monitor = _paced_monitor(
        _Supervisor(), _Cell(DaemonProbe.down("no")), database=database, clock=clock
    )
    await _spend_the_budget(monitor, clock)
    clock.now += 60
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_a_database_that_fails_again_withdraws_the_grant(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    unit = _Cell(DaemonProbe.down("no"))
    monitor = _paced_monitor(_Supervisor(), unit, database=database, clock=clock)
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
    monitor = _paced_monitor(
        _Supervisor(), _Cell(DaemonProbe.down("no")), database=_raising, clock=clock
    )
    await _spend_the_budget(monitor, clock)
    for _ in range(3):
        clock.now += 60
        await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    assert monitor.snapshot()["svc"].breaker_open


async def test_a_held_down_unit_stops_the_database_dial(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    supervisor = _Supervisor()
    monitor = _paced_monitor(
        supervisor, _Cell(DaemonProbe.down("no")), database=database, clock=clock
    )
    await _spend_the_budget(monitor, clock)
    supervisor.deferrals["svc"] = "held down"
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    dials = database.calls
    await monitor.run_round()
    assert database.calls == dials


# meiva:note
# Lease courtesy (task #5551) runs on real time: a window a round could outlast must be
# long, and a test that wants the window over sleeps past a short one. Nothing here
# patches the module clock.
LEASE_EVENT = "root_health_restart_lease_grace"
OPEN_WINDOW_S = 5.0
SHORT_WINDOW_S = 0.05
PAST_THE_WINDOW_S = 0.12


def _lease_monitor(
    supervisor: _Supervisor,
    probe: Callable[[], DaemonProbe],
    *,
    lease_probe: Callable[[], tuple[str, str] | None] | None,
    lease_grace_s: float = OPEN_WINDOW_S,
) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "max_restart_attempts": 3,
        "restart_backoff_s": (0.0,),
        "lease_grace_s": lease_grace_s,
    }
    return HealthMonitor(
        supervisor, registry, config=HealthConfig(**values), lease_probe=lease_probe
    )


def _live_lease() -> tuple[str, str]:
    return ("6f1d2c3a-lease", "external_agent:antigravity / Gemini: takeover")


async def test_the_first_restart_yields_once_to_a_live_lease(recorder: _Recorder) -> None:
    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor, _Cell(DaemonProbe.down("healthz unreachable")), lease_probe=_live_lease
    )
    await monitor.run_round()
    assert supervisor.restart_calls == []
    held = recorder.events(LEASE_EVENT)
    assert [(e["unit"], e["outcome"], e["attempt"], e["waited_s"]) for e in held] == [
        ("svc", "deferred", 1, OPEN_WINDOW_S)
    ]
    assert held[0]["lease_id"] == "6f1d2c3a-lease"
    assert held[0]["holder"] == "external_agent:antigravity / Gemini: takeover"
    assert held[0]["level"] == "warning"
    # meiva:note
    # The courtesy buys time, it does not spend the outage restart budget.
    assert monitor.snapshot()["svc"].restart_attempts == 0
    await monitor.run_round()
    assert supervisor.restart_calls == []
    assert len(recorder.events(LEASE_EVENT)) == 1


async def test_the_window_ending_still_restarts(recorder: _Recorder) -> None:
    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor,
        _Cell(DaemonProbe.down("healthz unreachable")),
        lease_probe=_live_lease,
        lease_grace_s=SHORT_WINDOW_S,
    )
    await monitor.run_round()
    assert supervisor.restart_calls == []
    await asyncio.sleep(PAST_THE_WINDOW_S)
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc"]
    assert monitor.snapshot()["svc"].restart_attempts == 1


async def test_a_later_attempt_of_the_same_outage_is_never_held(recorder: _Recorder) -> None:
    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor,
        _Cell(DaemonProbe.down("healthz unreachable")),
        lease_probe=_live_lease,
        lease_grace_s=SHORT_WINDOW_S,
    )
    await monitor.run_round()
    await asyncio.sleep(PAST_THE_WINDOW_S)
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc"]
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc", "svc"]
    assert len(recorder.events(LEASE_EVENT)) == 1


async def test_a_unit_that_answers_inside_the_window_cancels_the_restart(
    recorder: _Recorder,
) -> None:
    supervisor = _Supervisor()
    probe = _Cell(DaemonProbe.down("healthz unreachable"))
    monitor = _lease_monitor(supervisor, probe, lease_probe=_live_lease, lease_grace_s=1.0)
    await monitor.run_round()
    await asyncio.sleep(0.02)
    probe.verdict = DaemonProbe.up("ok")
    await monitor.run_round()
    assert supervisor.restart_calls == []
    held = recorder.events(LEASE_EVENT)
    assert [(e["outcome"], e["attempt"]) for e in held] == [
        ("deferred", 1),
        ("recovered", 1),
    ]
    # meiva:note
    # The opening line names the window it granted; the closing one names the wait that
    # actually happened, which is why they are not the same number.
    assert held[0]["waited_s"] == 1.0
    waited = held[1]["waited_s"]
    assert isinstance(waited, float)
    assert 0.0 < waited < 1.0
    await monitor.run_round()
    assert supervisor.restart_calls == []
    assert len(recorder.events(LEASE_EVENT)) == 2


async def test_a_broken_lease_probe_never_stops_a_restart(recorder: _Recorder) -> None:
    def broken() -> tuple[str, str] | None:
        raise RuntimeError("database unreachable")

    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor, _Cell(DaemonProbe.down("healthz unreachable")), lease_probe=broken
    )
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc"]
    assert recorder.events(LEASE_EVENT) == []


async def test_no_lease_means_the_restart_is_not_delayed(recorder: _Recorder) -> None:
    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor, _Cell(DaemonProbe.down("healthz unreachable")), lease_probe=lambda: None
    )
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc"]
    assert recorder.events(LEASE_EVENT) == []


async def test_zero_disables_the_courtesy(recorder: _Recorder) -> None:
    supervisor = _Supervisor()
    monitor = _lease_monitor(
        supervisor,
        _Cell(DaemonProbe.down("healthz unreachable")),
        lease_probe=_live_lease,
        lease_grace_s=0.0,
    )
    await monitor.run_round()
    assert supervisor.restart_calls == ["svc"]
    assert recorder.events(LEASE_EVENT) == []


async def test_held_down_without_an_operator_record_stays_quiet(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.down("no")), clock=clock)
    supervisor.deferrals["svc"] = "held down"
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    for _ in range(3):
        await monitor.run_round()
    assert not recorder.events("root_unit_operator_stop_unattended")


async def test_unattended_operator_stop_reports_once_then_again_for_a_new_stop(
    clock: _Clock, recorder: _Recorder
) -> None:
    now = time.time()
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.down("no")), clock=clock)
    supervisor.deferrals["svc"] = "held down"

    supervisor.facts = UnitFailureFacts(
        intent_running=False,
        restart_failed=None,
        intent_source=IntentSource.OPERATOR,
        intent_since=now - 3600.0,
    )
    for _ in range(3):
        await monitor.run_round()
    assert not recorder.events("root_unit_operator_stop_unattended")

    supervisor.facts = UnitFailureFacts(
        intent_running=False,
        restart_failed=None,
        intent_source=IntentSource.OPERATOR,
        intent_since=now - 7 * 3600.0,
    )
    for _ in range(3):
        await monitor.run_round()
    reports = recorder.events("root_unit_operator_stop_unattended")
    assert len(reports) == 1
    assert reports[0]["unit"] == "svc"
    assert supervisor.restart_calls == []
