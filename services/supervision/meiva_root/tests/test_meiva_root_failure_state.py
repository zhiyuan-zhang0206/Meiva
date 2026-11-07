from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest

from base.clock import SYSTEM_TIMERS, Clock, Timers
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
from services.supervision.meiva_root.health import HealthConfig, HealthGate, HealthMonitor
from services.supervision.meiva_root.intent_store import IntentSource, RestartFailure, RestartStage
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.probes import ProbeRegistry
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.meiva_root.tests.slices import clock_over

_CHILD = Path(__file__).with_name("child_unit.py")


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


# meiva:note
# Absence is the third state and the weakest claim: it reports "intent says running and
# nothing answers", which is only the finding when no recorded failure explains it. It must
# lose to both recorded states, and it must still be gated by the intent — a stopped unit that
# is not running is a decision, not an absence (task #6345).
def test_derive_kind_reports_absence_and_ranks_it_last() -> None:
    absent = UnitFailureFacts(intent_running=True, restart_failed=None, observed_absent=True)
    assert derive_kind(absent, restart_limit_reached=False) is FailureKind.ABSENT
    assert derive_kind(absent, restart_limit_reached=True) is FailureKind.BREAKER_OPEN
    both = UnitFailureFacts(intent_running=True, restart_failed=_failure(), observed_absent=True)
    assert derive_kind(both, restart_limit_reached=False) is FailureKind.RESTART_FAILED
    stopped = UnitFailureFacts(intent_running=False, restart_failed=None, observed_absent=True)
    assert derive_kind(stopped, restart_limit_reached=False) is None


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
    absent = UnitFailureFacts(intent_running=True, restart_failed=None, observed_absent=True)
    assert describe(absent, FailureKind.ABSENT) == "intent says running; no process is running"
    assert describe(absent, FailureKind.ABSENT, "pid 123 is gone") == "pid 123 is gone"


class _Supervisor:
    generation: tuple[OwnedProcess, float] | None

    def __init__(self) -> None:
        self.generation = (OwnedProcess(42, 100.0, None), 0.0)
        self.facts = UnitFailureFacts(intent_running=True, restart_failed=None)
        self.deferrals: dict[str, str] = {}
        self.restart_calls: list[str] = []
        self.input_drift: tuple[str, ...] = ()

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return self.deferrals.get(unit_id)

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        return self.facts

    def unit_input_drift(self, unit_id: str) -> tuple[str, ...]:
        return self.input_drift


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

    def monotonic(self) -> float:
        self.now += 0.02
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    async def asleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    # meiva:note
    # Move the fake clock to the next deadline the test cares about; a monitor reads time through
    # its clock, so the fake is steered here rather than by assigning a fixture attribute.
    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _monitor(
    supervisor: _Supervisor,
    probe: Callable[[], DaemonProbe],
    *,
    timers: Timers = SYSTEM_TIMERS,
    gate: HealthGate | None = None,
) -> HealthMonitor:
    registry = ProbeRegistry()
    registry.register("svc", probe)
    values: dict[str, Any] = {
        "interval_s": 0.01,
        "startup_grace_s": 0.0,
        "max_restart_attempts": 3,
        "restart_backoff_s": (0.0,),
    }
    return HealthMonitor(
        supervisor,
        registry,
        config=HealthConfig(**values),
        gate=gate,
        clock=clock_over(timers),
    )


async def test_exhausted_attempts_are_reported_every_round_then_clear_on_alive(
    clock: _Clock, recorder: _Recorder
) -> None:
    probe = _Cell(DaemonProbe.down("no"))
    monitor = _monitor(_Supervisor(), probe, timers=clock)
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
    monitor = _monitor(_Supervisor(), probe, timers=clock)
    for _ in range(4):
        await monitor.run_round()
    state = recorder.events("root_unit_failure_state")[-1]
    assert state["kind"] == "breaker_open"
    assert holder in str(state["detail"])


async def test_recorded_restart_failure_is_a_state_signal(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")), timers=clock)
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
    monitor = _monitor(_Supervisor(), probe, timers=clock)
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
    timers: Timers = SYSTEM_TIMERS,
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
        clock=clock_over(timers),
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
        timers=clock,
    )
    await _spend_the_budget(monitor, clock)
    supervisor.generation = (OwnedProcess(43, 101.0, None), clock.now)
    database.verdict = DaemonProbe.up("database answered")
    clock.advance(60)
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    clock.advance(181)
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_a_recovery_grant_survives_a_terminal_observation(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    unit = _Cell(DaemonProbe.down("no"))
    monitor = _paced_monitor(_Supervisor(), unit, database=database, timers=clock)
    await _spend_the_budget(monitor, clock)
    unit.verdict = DaemonProbe.port_taken("alien daemon")
    database.verdict = DaemonProbe.up("database answered")
    clock.advance(60)
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    unit.verdict = DaemonProbe.down("no")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_the_first_database_observation_grants_exactly_one_attempt(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.up("database answered"))
    monitor = _paced_monitor(
        _Supervisor(), _Cell(DaemonProbe.down("no")), database=database, timers=clock
    )
    await _spend_the_budget(monitor, clock)
    clock.advance(60)
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 4


async def test_a_database_that_fails_again_withdraws_the_grant(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    unit = _Cell(DaemonProbe.down("no"))
    monitor = _paced_monitor(_Supervisor(), unit, database=database, timers=clock)
    await _spend_the_budget(monitor, clock)
    unit.verdict = DaemonProbe.unavailable("inspection failed")
    database.verdict = DaemonProbe.up("database answered")
    clock.advance(60)
    await monitor.run_round()
    database.verdict = DaemonProbe.down("database unreachable again")
    await monitor.run_round()
    unit.verdict = DaemonProbe.down("no")
    await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3


async def test_a_raising_database_probe_never_releases_the_breaker(clock: _Clock) -> None:
    monitor = _paced_monitor(
        _Supervisor(), _Cell(DaemonProbe.down("no")), database=_raising, timers=clock
    )
    await _spend_the_budget(monitor, clock)
    for _ in range(3):
        clock.advance(60)
        await monitor.run_round()
    assert monitor.snapshot()["svc"].restart_attempts == 3
    assert monitor.snapshot()["svc"].breaker_open


async def test_a_held_down_unit_stops_the_database_dial(clock: _Clock) -> None:
    database = _Cell(DaemonProbe.down("database unreachable"))
    supervisor = _Supervisor()
    monitor = _paced_monitor(
        supervisor, _Cell(DaemonProbe.down("no")), database=database, timers=clock
    )
    await _spend_the_budget(monitor, clock)
    supervisor.deferrals["svc"] = "held down"
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    dials = database.calls
    await monitor.run_round()
    assert database.calls == dials


async def test_held_down_without_an_operator_record_stays_quiet(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    monitor = _monitor(supervisor, _Cell(DaemonProbe.down("no")), timers=clock)
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
    monitor = _monitor(supervisor, _Cell(DaemonProbe.down("no")), timers=clock)
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


# meiva:note
# Absence is the one kind derived from nothing being there, so it must hold across rounds
# before it is reported (task #6345): three rounds, then the ordinary every-round report. A
# round in which the unit is not absent resets the count, so two separated absences are
# never spent as one.
async def test_absence_is_reported_only_after_consecutive_rounds(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    supervisor.facts = UnitFailureFacts(
        intent_running=True, restart_failed=None, observed_absent=True
    )
    monitor = _monitor(
        supervisor,
        _Cell(DaemonProbe.down("no")),
        timers=clock,
        gate=lambda: (False, "an operator holds this window"),
    )
    await monitor.run_round()
    await monitor.run_round()
    assert recorder.events("root_unit_failure_state") == [], "two rounds is not absence yet"
    await monitor.run_round()
    assert [(s["kind"], s["detail"]) for s in recorder.events("root_unit_failure_state")] == [
        ("absent", "no")
    ]
    await monitor.run_round()
    assert len(recorder.events("root_unit_failure_state")) == 2, "then it reports every round"

    supervisor.facts = UnitFailureFacts(intent_running=True, restart_failed=None)
    await monitor.run_round()
    await monitor.run_round()
    await monitor.run_round()
    assert len(recorder.events("root_unit_failure_state")) == 2, "a present round clears it"


# meiva:note
# An absence whose gap is filled by a process this root does not own is already its own
# finding (`root_unit_unowned_alive`, task #5474), and reporting it a second time as a
# failure state would name the wrong cause (task #6345).
async def test_absence_is_not_reported_when_a_leftover_answers_the_probe(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    supervisor.generation = None
    supervisor.facts = UnitFailureFacts(
        intent_running=True, restart_failed=None, observed_absent=True
    )
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("someone answers")), timers=clock)
    for _ in range(4):
        await monitor.run_round()
    assert recorder.events("root_unit_failure_state") == []
    assert len(recorder.events("root_unit_unowned_alive")) == 1


# meiva:note
# A unit whose policy forbids revival is reported every round as `root_unit_not_revivable`;
# its absence is the same fact, so it is not reported twice (task #6345).
async def test_absence_is_not_reported_when_the_policy_forbids_revival(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    supervisor.deferrals["svc"] = "policy never"
    supervisor.facts = UnitFailureFacts(
        intent_running=True, restart_failed=None, observed_absent=True
    )
    monitor = _monitor(supervisor, _Cell(DaemonProbe.down("no")), timers=clock)
    for _ in range(4):
        await monitor.run_round()
    assert recorder.events("root_unit_failure_state") == []
    assert len(recorder.events("root_unit_not_revivable")) == 4
    assert supervisor.restart_calls == []


# meiva:note
# The seal drift is reported while the unit still looks healthy: the point of the signal is to
# arrive before something tries to spawn it and cannot (task #6403). A stopped unit is not
# reported — a unit nobody asked to run cannot be revived, which is not yet a degradation.
async def test_a_changed_sealed_input_is_reported_while_the_unit_is_healthy(
    clock: _Clock, recorder: _Recorder
) -> None:
    supervisor = _Supervisor()
    supervisor.input_drift = ("/home/u/.meiva/lgtm/native/config",)
    monitor = _monitor(supervisor, _Cell(DaemonProbe.up("ok")), timers=clock)
    await monitor.run_round()
    await monitor.run_round()
    reports = recorder.events("root_unit_inputs_changed")
    assert [(r["unit"], r["path"], r["level"]) for r in reports] == [
        ("svc", "/home/u/.meiva/lgtm/native/config", "warning"),
        ("svc", "/home/u/.meiva/lgtm/native/config", "warning"),
    ]
    supervisor.facts = UnitFailureFacts(intent_running=False, restart_failed=None)
    await monitor.run_round()
    assert len(recorder.events("root_unit_inputs_changed")) == 2, "a stop is a decision"


def _real_root(tmp_path: Path, clock: Clock, *scenario: str) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
    )
    return Supervisor(
        UnitRegistry([unit]),
        environ=dict(os.environ),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.2),
        clock=clock,
        root_pid=os.getpid(),
    )


# meiva:note
# The production probe shape for a unit that serves no port: does the generation this root owns
# still answer as the process it recorded? A leftover of some other root answers nothing here.
def _owned_identity_probe(owner: Supervisor) -> Callable[[], DaemonProbe]:
    def probe() -> DaemonProbe:
        generation = owner.health_generation("worker")
        if generation is None:
            return DaemonProbe.down("no owned generation answers")
        identity, _started_at = generation
        try:
            alive = identity.birth_matches(psutil.Process(identity.pid))
        except psutil.NoSuchProcess:
            alive = False
        if alive:
            return DaemonProbe.up("the owned generation answers")
        return DaemonProbe.down("the owned pid is gone")

    return probe


def _owned_probes(owner: Supervisor) -> ProbeRegistry:
    registry = ProbeRegistry()
    registry.register("worker", _owned_identity_probe(owner))
    return registry


# meiva:note
# The exit is recorded by the generation's own watcher, a separate task: `observed_absent` turns
# true as soon as the child is reaped, one scheduling step before `last_error` names the exit.
# Waiting for the recording, not only for the absence, keeps the assertions off that race.
async def _wait_recorded_exit(owner: Supervisor, timeout_s: float = 5.0) -> UnitFailureFacts:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        facts = owner.unit_failure_facts("worker")
        if facts.observed_absent and facts.last_error is not None:
            return facts
        await asyncio.sleep(0.01)
    raise AssertionError("the child never recorded its own exit")


# meiva:note
# The positive control the production fleet cannot stage (task #6345): a real supervisor holds no
# generation for a unit whose children die on their own, nothing records a replacement failure, and
# only the third consecutive absent round reaches the alert. The repair is a file rather than a new
# manifest, so the same unit proves the clearing edge too.
async def test_a_crashing_unit_reaches_absent_in_real_processes(
    clock: _Clock, recorder: _Recorder, tmp_path: Path
) -> None:
    hold = tmp_path / "hold"
    timers = clock_over(clock)
    owner = _real_root(tmp_path, timers, "exit_soon", "0.5", str(hold))
    await owner.start()
    try:
        facts = await _wait_recorded_exit(owner)
        assert facts.restart_failed is None, "a child that died on its own records no failure"
        assert facts.last_error == "unexpected exit: exit 1"

        monitor = HealthMonitor(
            owner,
            _owned_probes(owner),
            config=HealthConfig(
                interval_s=0.01,
                startup_grace_s=0.0,
                max_restart_attempts=3,
                restart_backoff_s=(60.0, 300.0, 900.0),
            ),
            clock=timers,
        )

        await monitor.run_round()
        assert monitor.snapshot()["worker"].restart_attempts == 1
        await _wait_recorded_exit(owner)

        clock.advance(60.0)
        await monitor.run_round()
        assert monitor.snapshot()["worker"].restart_attempts == 2
        await _wait_recorded_exit(owner)

        for _ in range(2):
            clock.advance(60.0)
            await monitor.run_round()
        assert recorder.events("root_unit_failure_state") == [], "two rounds is not absence yet"

        clock.advance(60.0)
        await monitor.run_round()
        assert [
            (event["unit"], event["kind"], event["detail"])
            for event in recorder.events("root_unit_failure_state")
        ] == [("worker", "absent", "no owned generation answers")]

        hold.touch()
        clock.advance(300.0)
        await monitor.run_round()
        assert [event["kind"] for event in recorder.events("root_unit_failure_state")] == [
            "absent",
            "breaker_open",
        ], "the third attempt spends the budget before readiness is observed"

        clock.advance(60.0)
        await monitor.run_round()
        assert monitor.snapshot()["worker"].restart_attempts == 0
        assert monitor.snapshot()["worker"].absent_rounds == 0
        assert len(recorder.events("root_unit_breaker_closed")) == 1
        clock.advance(60.0)
        await monitor.run_round()
        assert len(recorder.events("root_unit_failure_state")) == 2, "a repaired unit stays silent"
    finally:
        await owner.shutdown()
