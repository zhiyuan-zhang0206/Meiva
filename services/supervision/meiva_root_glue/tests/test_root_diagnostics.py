from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from threading import Event
from typing import NoReturn, cast
from unittest.mock import Mock

import pytest

from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.health import DaemonProbe, ProbeVerdict
from base.db import Database
from base.host.db_authority.api import publish_telemetry_token, telemetry_token
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.station_endpoint import StationEndpointConfig
from services.supervision.meiva_root.health import ProbeRunner
from services.supervision.meiva_root_glue import diagnostic_probes as probes
from services.supervision.meiva_root_glue.diagnostics import (
    Diagnostic,
    DiagnosticMonitor,
    RootHealthRounds,
)
from services.supervision.meiva_root_glue.tests.slices import probe_config, system_clock


class _Events:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    def _fields(self) -> list[dict[str, object]]:
        return [dict(r["extra"]) for r in self._records if "event" in r["extra"]]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._fields())

    def __getitem__(self, index: int) -> dict[str, object]:
        return self._fields()[index]

    def clear(self) -> None:
        self._records.clear()


def _unused_database() -> NoReturn:
    raise AssertionError("this diagnostic must not construct a database")


class _NoOpTelemetry:
    def start(
        self,
        *,
        name: str,
        producer: object,
        machine_reader: object,
        image: LoadedCommit,
    ) -> None:
        del name, producer, machine_reader, image

    def sync(self, *, timeout: float) -> object:
        del timeout
        return None


_NO_OP_TELEMETRY = _NoOpTelemetry()


@pytest.fixture
def events(loguru_records: LogRecords) -> _Events:
    return _Events(loguru_records)


def _entry(snapshot: dict[str, object], key: str) -> dict[str, object]:
    value = snapshot[key]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


async def test_deadline_retains_one_worker_and_discards_late_green() -> None:
    entered, release, exited = Event(), Event(), Event()
    calls = 0

    def stuck() -> DaemonProbe:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2)
        exited.set()
        return DaemonProbe.up("late green")

    runner = ProbeRunner()
    try:
        result = await runner.observe(stuck, 0.01)
        assert entered.is_set()
        assert result.verdict.value == "unavailable"
        result = await runner.observe(stuck, 0.01)
        assert result.verdict.value == "unavailable"
        assert calls == 1
    finally:
        release.set()
    while not exited.is_set():
        await asyncio.sleep(0.001)
    result = await runner.observe(lambda: DaemonProbe.down("fresh failure"), 0.1)
    assert result.verdict.value == "down"
    assert result.detail == "fresh failure"


async def test_probe_bug_is_unknown_evidence() -> None:
    def broken() -> DaemonProbe:
        raise OSError("inspection denied")

    result = await ProbeRunner().observe(broken, 0.1)
    assert result.verdict.value == "unavailable"


@pytest.mark.parametrize("verdict", list(ProbeVerdict))
async def test_probe_runner_preserves_the_protocol_result(verdict: ProbeVerdict) -> None:
    result = DaemonProbe(verdict, "application unready")
    runner = ProbeRunner()
    try:
        observed = await runner.observe(lambda: result, 1.0)
        assert observed is result
        assert observed.detail == "application unready"
    finally:
        assert runner.stop()


async def test_slow_diagnostic_cannot_block_peer_or_duplicate_itself(
    events: _Events,
) -> None:
    release = Event()
    calls = 0

    def blocked() -> DaemonProbe:
        nonlocal calls
        calls += 1
        release.wait(2)
        return DaemonProbe.up("late")

    monitor = DiagnosticMonitor(
        [
            Diagnostic("blocked", blocked, interval_s=0.001, timeout_s=0.01),
            Diagnostic("peer", lambda: DaemonProbe.down("observed failure"), interval_s=0.001),
        ],
        clock=system_clock(),
    )
    try:
        before = _entry(monitor.health_snapshot(), "diagnostic:blocked")
        assert before["sampled_at"] is None
        assert before["last_verdict"] is None
        await monitor.run_round()
        await asyncio.sleep(0.002)
        await monitor.run_round()
        snapshot = monitor.health_snapshot()
        assert _entry(snapshot, "diagnostic:blocked")["last_verdict"] == "unavailable"
        assert _entry(snapshot, "diagnostic:peer")["last_verdict"] == "down"
        assert calls == 1
    finally:
        release.set()


async def test_reporting_never_races_a_settling_worker(caplog: pytest.LogCaptureFixture) -> None:
    entered, release_probe = Event(), Event()
    tail_entered, release_tail = Event(), Event()

    def probe() -> DaemonProbe:
        entered.set()
        release_probe.wait(2)
        return DaemonProbe.up("fresh sample")

    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [Diagnostic("held", probe, interval_s=0.001, timeout_s=5.0, report=reported.append)],
        clock=system_clock(),
    )
    runner = monitor._states["held"].runner
    caplog.set_level(logging.ERROR, logger="services.supervision.meiva_root_glue.diagnostics")
    round_task = asyncio.create_task(monitor.run_round())
    try:
        await asyncio.to_thread(entered.wait, 2)
        assert entered.is_set()
        observation = runner._observation
        assert observation is not None

        def hold_tail(_future: object) -> None:
            tail_entered.set()
            release_tail.wait(5)

        observation.future.add_done_callback(hold_tail)
        release_probe.set()
        await asyncio.to_thread(tail_entered.wait, 2)
        await round_task
        assert [result.detail for result in reported] == ["fresh sample"]
        assert "reporting unavailable" not in caplog.text
    finally:
        release_probe.set()
        release_tail.set()
        await round_task


async def test_a_hanging_report_retains_its_check_worker_without_overlap() -> None:
    entered, release, done = Event(), Event(), Event()
    probe_calls = 0
    report_calls = 0

    def probe() -> DaemonProbe:
        nonlocal probe_calls
        probe_calls += 1
        return DaemonProbe.down("observed failure")

    def report(_result: DaemonProbe) -> None:
        nonlocal report_calls
        report_calls += 1
        entered.set()
        try:
            release.wait(5)
        finally:
            done.set()

    monitor = DiagnosticMonitor(
        [
            Diagnostic("held", probe, interval_s=0.001, timeout_s=0.05, report=report),
            Diagnostic("peer", lambda: DaemonProbe.up("ok"), interval_s=0.001),
        ],
        clock=system_clock(),
    )
    try:
        await monitor.run_round()
        assert report_calls == 1
        await asyncio.to_thread(entered.wait, 2)
        await monitor.run_round()
        assert probe_calls == 1 and report_calls == 1
        snapshot = monitor.health_snapshot()
        assert _entry(snapshot, "diagnostic:held")["last_verdict"] == "unavailable"
        assert _entry(snapshot, "diagnostic:peer")["last_verdict"] == "alive"
        release.set()
        await asyncio.to_thread(done.wait, 2)
        await asyncio.sleep(0.05)
        await monitor.run_round()
        assert probe_calls == 2 and report_calls == 2
    finally:
        release.set()


async def test_a_report_exception_keeps_the_fresh_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:

    def broken_report(_result: DaemonProbe) -> None:
        raise RuntimeError("alert backend down")

    monitor = DiagnosticMonitor(
        [
            Diagnostic(
                "canary",
                lambda: DaemonProbe.down("observed failure"),
                interval_s=0.001,
                report=broken_report,
            )
        ],
        clock=system_clock(),
    )
    caplog.set_level(logging.ERROR, logger="services.supervision.meiva_root_glue.diagnostics")
    await monitor.run_round()
    state = _entry(monitor.health_snapshot(), "diagnostic:canary")
    assert state["last_verdict"] == "down"
    assert state["consecutive_failures"] == 1
    assert "reporting failed" in caplog.text
    assert "alert backend down" in caplog.text


async def test_unknown_never_resolves_station_alert_but_extends_the_streak(
    events: _Events,
) -> None:
    current = DaemonProbe.down("canary failed")
    reported: list[DaemonProbe] = []
    monitor = DiagnosticMonitor(
        [
            Diagnostic(
                "canary",
                lambda: current,
                interval_s=0.001,
                report=reported.append,
            )
        ],
        clock=system_clock(),
    )
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1]
    current = DaemonProbe.unavailable("no host baseline")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert len(reported) == 1
    state = _entry(monitor.health_snapshot(), "diagnostic:canary")
    assert state["consecutive_failures"] == 2
    assert state["last_verdict"] == "unavailable"
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1, 2, 3]


async def test_every_failing_sample_reports_and_recovery_reports_once(
    events: _Events,
) -> None:
    current = DaemonProbe.unavailable("host stalled")
    monitor = DiagnosticMonitor(
        [Diagnostic("canary", lambda: current, interval_s=0.001)], clock=system_clock()
    )
    for _ in range(2):
        await monitor.run_round()
        await asyncio.sleep(0.002)
    assert [record["consecutive_failures"] for record in events] == [1, 2]
    assert all(record["event"] == "root_diagnostic" for record in events)
    current = DaemonProbe.up("healthy again")
    await monitor.run_round()
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1, 2, 0]
    assert events[-1]["verdict"] == "alive"
    current = DaemonProbe.unavailable("host stalled")
    await asyncio.sleep(0.002)
    await monitor.run_round()
    assert [record["consecutive_failures"] for record in events] == [1, 2, 0, 1]


class _Health:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.fail = False

    async def run_round(self) -> None:
        await self.release.wait()
        if self.fail:
            raise RuntimeError("round coordinator failed")

    def health_snapshot(self) -> dict[str, object]:
        return {"service": {"last_verdict": "down"}}

    async def stop(self) -> None:
        return None


async def test_tick_requires_both_rounds_and_does_not_mean_healthy(
    events: _Events, *, machine_identity: MachineIdentityReader
) -> None:
    health = _Health()
    diagnostics = DiagnosticMonitor(
        [Diagnostic("failed", lambda: DaemonProbe.down("failed"))], clock=system_clock()
    )
    rounds = RootHealthRounds(
        health,
        diagnostics,
        clients=ClientSet(),
        image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
        machine=machine_identity,
        telemetry=_NO_OP_TELEMETRY,
        clock=system_clock(),
        home=Path("/unused"),
    )
    task = asyncio.create_task(rounds.run_round())
    await asyncio.sleep(0.01)
    assert not any(event["event"] == "root_health_tick" for event in events)
    health.release.set()
    await task
    assert any(event["event"] == "root_health_tick" for event in events)
    assert _entry(rounds.health_snapshot(), "diagnostic:failed")["last_verdict"] == "down"
    events.clear()
    health.fail = True
    with pytest.raises(RuntimeError):
        await rounds.run_round()
    assert not any(event["event"] == "root_health_tick" for event in events)


async def test_expectation_precedes_first_sample_and_is_retired_on_stop(
    events: _Events, *, machine_identity: MachineIdentityReader
) -> None:
    async with asyncio.TaskGroup() as tasks:
        health = _Health()
        rounds = RootHealthRounds(
            health,
            DiagnosticMonitor([], clock=system_clock()),
            clients=ClientSet(),
            image=LoadedCommit(source_root=Path(__file__).resolve().parents[4], sha=None),
            tasks=tasks,
            machine=machine_identity,
            telemetry=_NO_OP_TELEMETRY,
            clock=system_clock(),
            home=Path("/unused"),
        )
        await rounds.start()
        assert events[0]["event"] == "root_health_expected"
        since = events[0]["expected_since_timestamp_seconds"]
        assert isinstance(since, float) and since > 0
        home_id = events[0]["home_id"]
        assert isinstance(home_id, str) and len(home_id) == 64
        assert _entry(rounds.health_snapshot(), "observer:root-health")["last_completed_at"] is None
        await rounds.stop()
        assert events[-1]["event"] == "root_health_expected"
        assert events[-1]["expected_since_timestamp_seconds"] == 0
        assert events[-1]["home_id"] == events[0]["home_id"]
        assert not any(event["event"] == "root_health_tick" for event in events)


@pytest.mark.parametrize("sha", ["captured-root-image", None])
async def test_root_logging_owns_only_the_started_pipeline(
    sha: str | None,
    tmp_path: Path,
    events: _Events,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    del events
    image = LoadedCommit(source_root=tmp_path, sha=sha)
    made: list[EventPipeline] = []

    def producer() -> EventPipeline:
        pipeline = EventPipeline(writer=lambda _events: None)
        made.append(pipeline)
        return pipeline

    clients = ClientSet(pipeline_factory=producer)
    seen: list[LoadedCommit] = []

    class _RecordingTelemetry(_NoOpTelemetry):
        def start(
            self,
            *,
            name: str,
            producer: object,
            machine_reader: object,
            image: LoadedCommit,
        ) -> None:
            assert name == "meiva-root"
            assert callable(producer) and callable(machine_reader)
            assert producer() is clients.event_pipeline()
            seen.append(image)

    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(),
            DiagnosticMonitor([], clock=system_clock()),
            clients=clients,
            image=image,
            tasks=tasks,
            machine=machine_identity,
            telemetry=_RecordingTelemetry(),
            clock=system_clock(),
            home=Path("/unused"),
        )
        assert made == []
        await rounds.start()
        assert seen == [image] and len(made) == 1
        await rounds.stop()
        assert made[0].stopped


async def test_logging_startup_error_stops_its_constructed_writer(
    tmp_path: Path, *, machine_identity: MachineIdentityReader
) -> None:
    made: list[EventPipeline] = []
    error = ValueError("root logging failed after constructing its writer")

    def producer() -> EventPipeline:
        pipeline = EventPipeline(writer=lambda _events: None)
        made.append(pipeline)
        return pipeline

    clients = ClientSet(pipeline_factory=producer)

    class _FailingTelemetry(_NoOpTelemetry):
        def start(
            self,
            *,
            name: str,
            producer: object,
            machine_reader: object,
            image: LoadedCommit,
        ) -> NoReturn:
            del name, producer, machine_reader, image
            clients.event_pipeline()
            raise error

    async with asyncio.TaskGroup() as tasks:
        rounds = RootHealthRounds(
            _Health(),
            DiagnosticMonitor([], clock=system_clock()),
            clients=clients,
            image=LoadedCommit(source_root=tmp_path, sha=None),
            tasks=tasks,
            machine=machine_identity,
            telemetry=_FailingTelemetry(),
            clock=system_clock(),
            home=Path("/unused"),
        )
        with pytest.raises(ValueError) as caught:
            await rounds.start()
        assert caught.value is error and len(made) == 1
        assert made[0].stopped


def test_helper_diagnostics_are_macos_only(*, machine_identity: ConfiguredMachineIdentity) -> None:
    machine_identity.set(role="agent-runner")
    names = {
        check.name
        for check in probes.build_diagnostics(
            set(),
            database=_unused_database,
            machine=machine_identity,
            macos=False,
            clock=system_clock(),
            config=probe_config(),
        )
    }
    assert names == {"venv"}
    names = {
        check.name
        for check in probes.build_diagnostics(
            set(),
            database=_unused_database,
            machine=machine_identity,
            macos=True,
            clock=system_clock(),
            config=probe_config(),
        )
    }
    assert names == {"venv", "brew-pin", "permissions-helper"}


def test_station_no_credential_is_unknown_and_does_not_send(unit_home: Path) -> None:
    publish_telemetry_token(unit_home, "other-home-token")
    opener = FakeUrlOpener()
    create = Mock(side_effect=AssertionError("missing credentials must not open a database"))
    probe = probes.StationProbe(
        database=create,
        config=StationEndpointConfig("http://station.invalid", 4325),
        home=unit_home / "unpublished-home",
        open_url=opener,
    )
    assert probe.probe().verdict.value == "unavailable"
    assert opener.calls == []
    create.assert_not_called()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (Answer(status=202), ProbeVerdict.ALIVE),
        (Answer(status=401), ProbeVerdict.DOWN),
        (OSError("offline"), ProbeVerdict.DOWN),
    ],
)
def test_station_observes_the_authenticated_ingress(
    unit_home: Path, database: Database, answer: Answer | OSError, expected: ProbeVerdict
) -> None:
    publish_telemetry_token(unit_home, "station-test-token")
    opener = FakeUrlOpener()
    opener.on("http://station.invalid:4325/v1/traces", answer)
    probe = probes.StationProbe(
        database=lambda: database,
        config=StationEndpointConfig("http://station.invalid", 4325),
        home=unit_home,
        open_url=opener,
    )
    assert probe.probe().verdict is expected
    assert len(opener.calls) == 1
    assert (
        opener.calls[0].headers["Authorization"]
        == f"Bearer {telemetry_token('station-test-token')}"
    )


def test_station_resolution_failure_is_unavailable_and_does_not_send(unit_home: Path) -> None:
    publish_telemetry_token(unit_home, "station-test-token")
    create = Mock(side_effect=RuntimeError("station registry unavailable"))
    opener = FakeUrlOpener()
    probe = probes.StationProbe(
        database=create,
        config=StationEndpointConfig("http://station.invalid", 4325),
        home=unit_home,
        open_url=opener,
    )
    assert probe.probe().verdict.value == "unavailable"
    create.assert_called_once_with()
    assert opener.calls == []


def test_missing_brew_probe_is_unavailable_not_an_empty_healthy_set(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probes.brew_pins(brew_binary=str(tmp_path / "missing-brew"))


def test_redis_acl_uses_runtime_ping_without_native_custody() -> None:
    class _FakeRedisConnection:
        def __init__(self) -> None:
            self.pings = 0

        def __enter__(self) -> _FakeRedisConnection:
            return self

        def __exit__(self, _exc_type: object, _exc: object, _tb: object, /) -> None:
            return None

        def ping(self) -> bool:
            self.pings += 1
            return True

    conn = _FakeRedisConnection()
    assert not hasattr(probes, "RedisConnectionCustody")
    assert probes.redis_acl(
        redis_url="redis://localhost:6380",
        connection=conn,
    ).alive
    assert conn.pings == 1


def test_each_root_roster_owns_its_helper_episode_reporter(
    events: _Events,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(role="agent-runner")
    first = next(
        check
        for check in probes.build_diagnostics(
            set(),
            database=_unused_database,
            machine=machine_identity,
            macos=True,
            clock=system_clock(),
            config=probe_config(),
        )
        if check.name == "permissions-helper"
    )
    second = next(
        check
        for check in probes.build_diagnostics(
            set(),
            database=_unused_database,
            machine=machine_identity,
            macos=True,
            clock=system_clock(),
            config=probe_config(),
        )
        if check.name == "permissions-helper"
    )
    assert first.report is not None and second.report is not None
    bad = DaemonProbe.down("lwcr-stuck; helper unavailable")
    first.report(bad)
    first.report(bad)
    second.report(bad)
    assert (
        len([event for event in events if event.get("event") == "permissions_helper_unhealthy"])
        == 2
    )


class _ProbeCursor:
    def __init__(self, error: BaseException | None, row: tuple[int, ...] = (1,)) -> None:
        self._error = error
        self._row = row
        self.executed: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        del params
        self.executed.append(sql)
        if self._error is not None:
            raise self._error

    def fetchone(self) -> tuple[int, ...]:
        return self._row

    def __enter__(self) -> _ProbeCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _ProbeConnection:
    def __init__(self, error: BaseException | None, row: tuple[int, ...] = (1,)) -> None:
        self._error = error
        self.autocommit: bool | None = None
        self.cursor_instance = _ProbeCursor(error, row)

    def cursor(self) -> _ProbeCursor:
        return self.cursor_instance

    def __enter__(self) -> _ProbeConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


class _ProbeDatabase:
    def __init__(self, error: BaseException | None = None, row: tuple[int, ...] = (1,)) -> None:
        self.connection = _ProbeConnection(error, row)
        self.dials = 0

    def connect(self, *, autocommit: bool = False) -> _ProbeConnection:
        self.dials += 1
        self.connection.autocommit = autocommit
        return self.connection


def test_database_recovery_probe_answers_with_select_one() -> None:
    database = _ProbeDatabase()
    result = probes.database_recovery_probe(lambda: cast("Database", database))()
    assert result.alive
    assert database.dials == 1
    assert database.connection.autocommit is True
    assert database.connection.cursor_instance.executed == ["SELECT 1"]


def test_database_recovery_probe_separates_unreachable_from_unobserved() -> None:
    import psycopg

    unreachable = _ProbeDatabase(psycopg.OperationalError("connection refused"))
    down = probes.database_recovery_probe(lambda: cast("Database", unreachable))()
    assert down.verdict.value == "down"
    assert "OperationalError" in down.detail

    unobserved = _ProbeDatabase(RuntimeError("gate refused"))
    unavailable = probes.database_recovery_probe(lambda: cast("Database", unobserved))()
    assert unavailable.verdict.value == "unavailable"
    assert "RuntimeError" in unavailable.detail


# meiva:note
# The reminder record: the digests the daemon delivered against the telemetry rows those
# deliveries emit. Both sides are in this test's one query, so a delivery without a stored
# row is the DOWN verdict while "nothing was due" stays healthy.
def test_task_telemetry_sees_deliveries_that_left_no_stored_row() -> None:
    database = _ProbeDatabase(row=(129, 0))
    result = probes.task_telemetry(lambda: cast("Database", database))()
    assert result.verdict == ProbeVerdict.DOWN
    assert "129 reminder notice(s) delivered in 24h" in result.detail
    assert "no task reminder or escalation row" in result.detail
    (sql,) = database.connection.cursor_instance.executed
    assert "inbound_messages" in sql and "telemetry_events" in sql
    assert "'task_reminder_digest', 'task_reminder_rerouted', 'task_escalation'" in sql


@pytest.mark.parametrize(("delivered", "stored"), [(129, 129), (0, 0)])
def test_task_telemetry_stays_healthy_with_a_stored_record_or_nothing_due(
    delivered: int, stored: int
) -> None:
    database = _ProbeDatabase(row=(delivered, stored))
    result = probes.task_telemetry(lambda: cast("Database", database))()
    assert result.verdict == ProbeVerdict.ALIVE
    assert f"{delivered} reminder notice(s) delivered" in result.detail
    assert f"{stored} task telemetry row(s) stored" in result.detail


def test_task_telemetry_is_unavailable_when_the_record_cannot_be_read() -> None:
    import psycopg

    database = _ProbeDatabase(psycopg.OperationalError("connection refused"))
    result = probes.task_telemetry(lambda: cast("Database", database))()
    assert result.verdict == ProbeVerdict.UNAVAILABLE
    assert "OperationalError" in result.detail


def test_the_reminder_record_is_observed_on_the_gateway_only(
    *, machine_identity: ConfiguredMachineIdentity
) -> None:
    def roster(role: str) -> dict[str, Diagnostic]:
        machine_identity.set(role=role)
        return {
            check.name: check
            for check in probes.build_diagnostics(
                set(),
                database=_unused_database,
                machine=machine_identity,
                macos=False,
                clock=system_clock(),
                config=probe_config(),
            )
        }

    assert "task-telemetry" not in roster("agent-runner")
    assert roster("gateway")["task-telemetry"].interval_s == 3600
