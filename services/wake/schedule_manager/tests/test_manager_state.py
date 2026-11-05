from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster import session_name
from base.config import settings
from base.daemon.schedules.claim import write_claim
from base.daemon.schedules.heartbeat import beat_path, write_beat
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.tests.serving_root import observe
from base.sessions.protocol import SessionBackend
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.wake.schedule_manager import manager as manager_module
from services.wake.schedule_manager import round_progress
from services.wake.schedule_manager.tests.manager_support import NOW, POLICY, schedule_manager


class _FakeBackend(SessionBackend):
    def __init__(self) -> None:
        self.live: list[str] = []
        self.created: list[str] = []
        self.launched: list[int] = []
        self.epoch: str | None = "host-a"

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def session_generation(self, name: str) -> str | None:
        return None

    def host_generation(self) -> str | None:
        return self.epoch

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        self.launched.append(int(str(env["MEIVA_SCHEDULE_ID"])))
        self.created.append(name)
        self.live.append(name)
        return True

    def kill_session(self, name: str, **_kw: object) -> tuple[bool, str]:
        self.live = [s for s in self.live if s != name]
        return True, "forced"


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool[psycopg.Connection]]:
    p: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def fake_session(
    serving_root: start_serving.RootBirth, unit_home: Path
) -> tuple[_FakeBackend, list[int]]:
    backend = _FakeBackend()
    generation = start_serving.begin_start(unit_home)
    assert (
        start_serving.mark_serving(
            unit_home, generation, runtime=serving_root.runtime, observe=observe
        )
        is True
    )
    return backend, backend.launched


def _insert(
    conn: psycopg.Connection, name: str, *, enabled: bool = True, machine: str = "gateway"
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, machine) "
            "VALUES (%s, 'x', 'python schedule.py', %s, 'stopped', %s) RETURNING id",
            (name, enabled, machine),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _status(conn: psycopg.Connection, sid: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _set_status(conn: psycopg.Connection, sid: int, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE schedules SET status = %s WHERE id = %s", (status, sid))
    conn.commit()


def _backoff_state(conn: psycopg.Connection, sid: int) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT launch_count, next_launch_at > clock_timestamp() FROM schedules WHERE id = %s",
        (sid,),
    ).fetchone()
    assert row is not None
    return row[0], bool(row[1])


def _end_backoff(conn: psycopg.Connection, sid: int) -> None:
    conn.execute(
        "UPDATE schedules SET next_launch_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (sid,),
    )
    conn.commit()


def _set_backoff(conn: psycopg.Connection, sid: int, count: int, *, seconds_ahead: float) -> None:
    conn.execute(
        "UPDATE schedules SET launch_count = %s, "
        "next_launch_at = clock_timestamp() + make_interval(secs => %s) WHERE id = %s",
        (count, seconds_ahead, sid),
    )
    conn.commit()


def _age_not_live(conn: psycopg.Connection, sid: int, seconds: float) -> None:
    conn.execute(
        "UPDATE schedules SET not_live_since = clock_timestamp() - make_interval(secs => %s) "
        "WHERE id = %s",
        (seconds, sid),
    )
    conn.commit()


def test_error_schedule_not_relaunched_after_restart(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "err-a")
    _set_status(db_conn, sid, "error")

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert launched == []
    assert _status(db_conn, sid) == "error"


def test_missing_error_schedule_alerts_after_two_hours_and_rearms_after_recovery(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    caplog: pytest.LogCaptureFixture,
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "stalled-error")
    _set_status(db_conn, sid, "error")
    manager = schedule_manager(pool, backend)

    with caplog.at_level(logging.WARNING, logger=round_progress.__name__):
        manager.reconcile(now=NOW)
        _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S - 5)
        manager.reconcile(now=NOW)
        assert len(emitted_events) == 0
        _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile(now=NOW)
        manager.reconcile(now=NOW)

        name = session_name(f"schedule-{sid}")
        backend.live.append(name)
        manager.reconcile(now=NOW)
        backend.live.remove(name)
        manager.reconcile(now=NOW)
        _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 0.1)
        manager.reconcile(now=NOW)

    emitted = [(event.event_name, event.attributes) for event in emitted_events]
    assert [(name, attrs["schedule_id"], attrs["status"]) for name, attrs in emitted] == [
        ("schedule_stalled", sid, "error"),
        ("schedule_stalled", sid, "error"),
    ]
    # meiva:note
    # No beat exists on this machine, so the only observable fact is the sessionless
    # observation itself and both durations name it.
    assert all(7200.1 <= cast(float, attrs["silent_seconds"]) < 7260 for _name, attrs in emitted)
    assert all(
        7200.1 <= cast(float, attrs["session_missing_seconds"]) < 7260 for _name, attrs in emitted
    )
    assert all("beat_written_at" not in attrs for _name, attrs in emitted)
    assert caplog.text.count(f"schedule {sid} has been seen with nothing running") == 2


def test_the_stall_alert_clock_and_flag_survive_a_restart(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "stall-restart")
    _set_status(db_conn, sid, "error")

    schedule_manager(pool, backend).reconcile(now=NOW)
    _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 1)
    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_stalled"]
    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_stalled"]


def test_completed_and_disabled_schedules_do_not_stall_alert(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    completed = _insert(db_conn, "completed-no-alert")
    _set_status(db_conn, completed, "completed")
    disabled = _insert(db_conn, "disabled-no-alert", enabled=False)
    for sid in (completed, disabled):
        _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 1)

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert len(emitted_events) == 0
    cleared = db_conn.execute(
        "SELECT count(*) FROM schedules WHERE not_live_since IS NULL AND stall_alerted_at IS NULL "
        "AND id = ANY(%s)",
        ([completed, disabled],),
    ).fetchone()
    assert cleared == (2,)


def test_a_launch_is_claimed_once(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "claim-once")
    mgr = schedule_manager(pool, backend)

    assert mgr._claim_launch(sid, 0, "host-a") is True
    assert mgr._claim_launch(sid, 0, "host-a") is False
    _end_backoff(db_conn, sid)
    assert mgr._claim_launch(sid, 0, "host-a") is False
    assert schedule_manager(pool, backend)._claim_launch(sid, 0, "host-a") is False
    assert _backoff_state(db_conn, sid) == (1, False)


def _insert_running(
    conn: psycopg.Connection, name: str, *, generation: str | None, machine: str = "gateway"
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, launch_count, "
            "launch_generation, machine) VALUES (%s, 'x', 'python schedule.py', true, 'running', 1, %s, %s) "
            "RETURNING id",
            (name, generation, machine),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _state(conn: psycopg.Connection, sid: int) -> tuple[str, str | None, int]:
    row = conn.execute(
        "SELECT status, launch_generation, launch_count FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _attributes(events: EmittedEvents) -> list[dict[str, object]]:
    return [dict(event.attributes) for event in events]


def test_a_replaced_session_host_relaunches_the_schedule_once(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-wave", generation="host-a")
    backend.epoch = "host-b"

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert _state(db_conn, sid) == ("running", "host-b", 1)
    assert backend.created == [session_name(f"schedule-{sid}")]
    assert emitted_events.names() == ["schedule_auto_restart"]
    assert _attributes(emitted_events) == [
        {
            "schedule_id": sid,
            "previous_generation": "host-a",
            "current_generation": "host-b",
            "restarts_in_window": 1,
        }
    ]


def test_a_second_silent_death_in_the_same_generation_is_terminal(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-wave-then-dies", generation="host-a")
    manager = schedule_manager(pool, backend)
    backend.epoch = "host-b"
    manager.reconcile(now=NOW)
    assert _state(db_conn, sid)[0] == "running"

    backend.live.clear()
    manager.reconcile(now=NOW)

    assert _state(db_conn, sid)[:2] == ("error", "host-b")
    assert len(backend.created) == 1


def test_an_unreachable_session_host_marks_nothing_lost(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-absent", generation="host-a")
    backend.epoch = None

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert _state(db_conn, sid) == ("running", "host-a", 1)
    assert backend.created == []
    still_clocked = db_conn.execute(
        "SELECT not_live_since IS NULL FROM schedules WHERE id = %s", (sid,)
    ).fetchone()
    assert still_clocked == (True,)


def test_an_attempt_from_before_the_column_is_not_relaunched(
    db_conn: psycopg.Connection, pool: ConnectionPool, fake_session: tuple[_FakeBackend, list[int]]
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-unknown-attempt", generation=None)
    backend.epoch = "host-b"

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert _state(db_conn, sid)[0] == "error"
    assert backend.created == []


def test_the_window_cap_stops_relaunching_a_host_that_keeps_restarting(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-storm", generation="gen-0")
    manager = schedule_manager(pool, backend)

    for index in range(POLICY.auto_restart_max_per_window):
        backend.epoch = f"gen-{index + 1}"
        manager.reconcile(now=NOW)
        assert _state(db_conn, sid)[0] == "running"
        backend.live.clear()

    backend.epoch = "gen-final"
    manager.reconcile(now=NOW)

    assert _state(db_conn, sid)[0] == "error"
    assert len(backend.created) == POLICY.auto_restart_max_per_window
    assert emitted_events.names() == [
        *(["schedule_auto_restart"] * POLICY.auto_restart_max_per_window),
        "schedule_auto_restart_suppressed",
        "schedule_process_lost",
    ]
    suppressed = next(
        attributes
        for name, attributes in zip(
            emitted_events.names(), _attributes(emitted_events), strict=True
        )
        if name == "schedule_auto_restart_suppressed"
    )
    assert suppressed["previous_generation"] == f"gen-{POLICY.auto_restart_max_per_window}"
    assert suppressed["current_generation"] == "gen-final"
    assert suppressed["restarts_in_window"] == POLICY.auto_restart_max_per_window


# meiva:note
# Losing the session is reported when it is seen, not two hours later. The update behind the event
# matches only a schedule still `running`, so the second pass has nothing left to say; the pair with
# `schedule_stalled` (the same outage, still unhandled two hours in) is asserted where the stall
# pass is tested.
def test_a_lost_session_is_reported_the_round_it_is_seen(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "same-host-loss")
    _set_status(db_conn, sid, "running")
    manager = schedule_manager(pool, backend)

    manager.reconcile(now=NOW)

    assert _state(db_conn, sid)[0] == "error"
    assert emitted_events.names() == ["schedule_process_lost"]
    assert _attributes(emitted_events) == [{"schedule_id": sid, "status": "error"}]

    manager.reconcile(now=NOW)

    assert emitted_events.names() == ["schedule_process_lost"]


def test_a_refused_launch_does_not_consume_the_auto_restart_budget(
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    unit_home: Path,
) -> None:
    backend, _launched = fake_session
    sid = _insert_running(db_conn, "host-gated", generation="host-a")
    backend.epoch = "host-b"
    manager = schedule_manager(pool, backend)
    start_serving.begin_start(unit_home)

    for _ in range(POLICY.auto_restart_max_per_window + 2):
        manager.reconcile(now=NOW)

    assert _state(db_conn, sid)[0] == "running"
    assert backend.created == []
    assert "schedule_auto_restart" not in emitted_events.names()
    assert "schedule_auto_restart_suppressed" not in emitted_events.names()


# meiva:note
# The round-deadline alert: a resident schedule states when it will be back, and the
# manager reports the promise it stopped keeping. Late, on time, no heartbeat at all,
# and a session already gone (which belongs to `schedule_stalled`, never to both).
def _write_beat(home: Path, sid: int, *, offset_s: float) -> None:
    now = datetime.now(UTC)
    write_beat(home, sid, written_at=now, alive_by=now + timedelta(seconds=offset_s))


def _write_beat_ago(home: Path, sid: int, *, written_s_ago: float) -> None:
    written = datetime.now(UTC) - timedelta(seconds=written_s_ago)
    write_beat(home, sid, written_at=written, alive_by=written + timedelta(seconds=600))


def _hold_live(backend: _FakeBackend, sid: int) -> None:
    name = session_name(f"schedule-{sid}")
    if name not in backend.live:
        backend.live.append(name)


def test_a_late_round_alerts_once_and_survives_a_restart(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "late-round")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    _write_beat(unit_home, sid, offset_s=-600)

    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_not_progressing"]
    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_not_progressing"]


def test_an_on_time_round_is_silent(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "on-time-round")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    _write_beat(unit_home, sid, offset_s=600)

    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == []


def test_a_schedule_without_a_heartbeat_is_silent(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "no-heartbeat")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    assert not beat_path(unit_home, sid).exists()

    schedule_manager(pool, backend).reconcile(now=NOW)
    assert emitted_events.names() == []


def test_a_sessionless_schedule_reports_the_stall_and_not_the_round(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "sessionless-late-round")
    _set_status(db_conn, sid, "running")
    _write_beat(unit_home, sid, offset_s=-600)

    schedule_manager(pool, backend).reconcile(now=NOW)
    _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 1)
    schedule_manager(pool, backend).reconcile(now=NOW)

    assert emitted_events.names() == ["schedule_process_lost", "schedule_stalled"]


def test_the_stall_report_measures_the_silence_not_the_detection(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "late-detection")
    _set_status(db_conn, sid, "error")
    _write_beat_ago(unit_home, sid, written_s_ago=5 * 3600)

    manager = schedule_manager(pool, backend)
    manager.reconcile(now=NOW)
    _age_not_live(db_conn, sid, round_progress.STALL_ALERT_AFTER_S + 1)
    manager.reconcile(now=NOW)

    (event,) = list(emitted_events)
    assert event.event_name == "schedule_stalled"
    # meiva:note
    # The manager noticed only one alert interval ago; the schedule stopped working five hours
    # ago. The reported silence is the second number, and the first says which clock it came from.
    assert 5 * 3600 <= cast(float, event.attributes["silent_seconds"]) < 5 * 3600 + 60
    assert (
        round_progress.STALL_ALERT_AFTER_S
        <= cast(float, event.attributes["session_missing_seconds"])
        < round_progress.STALL_ALERT_AFTER_S + 60
    )
    assert isinstance(event.attributes["beat_written_at"], str)


def test_a_launch_forgets_the_previous_attempts_round(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    db_conn: psycopg.Connection,
) -> None:
    backend, launched = fake_session
    sid = _insert(db_conn, "beat-cleared-on-launch")
    _write_beat_ago(unit_home, sid, written_s_ago=60)
    assert beat_path(unit_home, sid).exists()

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert sid in launched
    assert not beat_path(unit_home, sid).exists()


def test_a_fresh_round_rearms_the_alert(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "rearmed-round")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    _write_beat(unit_home, sid, offset_s=-600)

    mgr = schedule_manager(pool, backend)
    mgr.reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_not_progressing"]

    _write_beat(unit_home, sid, offset_s=600)
    mgr.reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_not_progressing"]

    _write_beat(unit_home, sid, offset_s=-900)
    mgr.reconcile(now=NOW)
    assert emitted_events.names() == ["schedule_not_progressing", "schedule_not_progressing"]


def _claim_a_runner_that_then_dies(home: Path, sid: int) -> None:
    # meiva:note
    # The production shape: a runner records itself, then is killed while its session's login
    # shell stays. The reader must prove the death from the process table, not from the session.
    proc = subprocess.Popen(["/bin/sleep", "60"])
    assert write_claim(home, sid, pid=proc.pid) is not None
    proc.kill()
    deadline = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_live_session_whose_runner_is_gone_is_a_lost_process(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "runner-gone")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    _claim_a_runner_that_then_dies(unit_home, sid)

    schedule_manager(pool, backend).reconcile(now=NOW)

    assert emitted_events.names() == ["schedule_process_lost"]
    assert _status(db_conn, sid) == "error"
    row = db_conn.execute("SELECT not_live_since FROM schedules WHERE id = %s", (sid,)).fetchone()
    assert row is not None and row[0] is not None


def test_a_session_with_no_runner_record_is_unknown_and_acted_on_by_nothing(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    caplog: pytest.LogCaptureFixture,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "runner-unrecorded")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)

    manager = schedule_manager(pool, backend)
    with caplog.at_level(logging.WARNING, logger=manager_module.__name__):
        manager.reconcile(now=NOW)
        manager.reconcile(now=NOW)

    # meiva:note
    # Unprovable is not dead: a session started before runners recorded themselves keeps today's
    # answer, and saying so once is the whole of the manager's response.
    assert emitted_events.names() == []
    assert _status(db_conn, sid) == "running"
    assert caplog.text.count("no provable runner") == 1
    assert f"schedules [{sid}]" in caplog.text


def test_a_session_whose_runner_is_alive_is_not_reported_as_unknown(
    unit_home: Path,
    pool: ConnectionPool[psycopg.Connection],
    fake_session: tuple[_FakeBackend, list[int]],
    emitted_events: EmittedEvents,
    caplog: pytest.LogCaptureFixture,
    db_conn: psycopg.Connection,
) -> None:
    backend, _launched = fake_session
    sid = _insert(db_conn, "runner-alive")
    _set_status(db_conn, sid, "running")
    _hold_live(backend, sid)
    # meiva:note
    # The test process stands in for the runner: it is alive, and the record describes it.
    assert write_claim(unit_home, sid, pid=os.getpid()) is not None

    with caplog.at_level(logging.WARNING, logger=manager_module.__name__):
        schedule_manager(pool, backend).reconcile(now=NOW)

    assert emitted_events.names() == []
    assert "no provable runner" not in caplog.text
    assert _status(db_conn, sid) == "running"
