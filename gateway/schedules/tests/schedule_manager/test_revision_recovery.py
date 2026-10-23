# meiva:note
# Convergence of a schedule's session to its durable desired revision.
#
# The session backend is a fake of `SessionBackend`; the launch provenance the manager reads back
# (the command a session was started with) comes from a pty-sessions service answering on the
# home's real socket (`FakePtyService`), so the manager reads provenance the way it does in
# production. A test that needs provenance to be unreadable simply leaves that service down.

from collections.abc import Generator
from pathlib import Path
from typing import Any

import psycopg
import pytest

import base.db
from base.cluster import session_name
from base.db.code_version_gate import ProcessDbGate
from base.deploy.lifecycle import start_serving
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty.paths import service_socket_path
from base.sessions.pty.tests.pty_service import FakePtyService
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.schedules import session_control
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.blocked_streak import BLOCKED_GIVE_UP_AFTER_S
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager
from services.wake.schedule_manager.tests.session_fakes import Backend


@pytest.fixture
def provenance(unit_home: Path) -> Generator[list[dict[str, Any]]]:
    """The pty-sessions service of the home, answering with the sessions the test's backend holds."""
    sessions: list[dict[str, Any]] = []
    socket_path = service_socket_path()
    socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    service = FakePtyService(socket_path, sessions)
    try:
        yield sessions
    finally:
        service.close()


@pytest.fixture
def backend(
    provenance: list[dict[str, Any]], serving_root: start_serving.RootBirth, unit_home: Path
) -> Backend:
    generation = start_serving.begin_start(unit_home)
    assert start_serving.mark_serving(unit_home, generation, runtime=serving_root.runtime)
    return Backend(provenance)


def _insert(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command, desired_revision) VALUES ('revision', 'pass', 'python schedule.py', 1) RETURNING id"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def _runner_command(sid: int, revision: int) -> str:
    return f".venv/bin/python -m services.wake.schedule_manager.runner {sid} {revision}; exit $?"


def test_acknowledgement_loss_adopts_session_across_manager_restart(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    with base.db.pool(gate=database_gate) as pool:
        session_control.enqueue_blocking(pool, sid)
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
        assert requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW) == 1
    assert backend.launches == 1
    assert backend.killed == []
    assert db_conn.execute(
        "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1, 1)


def test_crash_after_session_birth_before_applied_write_adopts_provenance(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    backend.hold(session_name(f"schedule-{sid}"), _runner_command(sid, 1))
    with base.db.pool(gate=database_gate) as pool:
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
    assert backend.launches == 0
    assert backend.killed == []
    assert db_conn.execute(
        "SELECT applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1,)


def test_a_session_launched_under_the_retired_entrypoint_is_replaced_not_adopted(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    name = session_name(f"schedule-{sid}")
    backend.hold(name, f".venv/bin/python -m gateway.schedules.runner {sid} 1; exit $?")
    with base.db.pool(gate=database_gate) as pool:
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
    assert backend.killed == [name]
    assert backend.launches == 1
    assert db_conn.execute(
        "SELECT applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1,)


def test_completed_applied_revision_is_not_rerun_by_unacknowledged_queue(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    db_conn.execute(
        "UPDATE schedules SET applied_revision = 1, status = 'completed' WHERE id = %s", (sid,)
    )
    db_conn.commit()
    with base.db.pool(gate=database_gate) as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW) == 1
    assert backend.launches == 0
    assert backend.killed == []


def test_live_adoption_preserves_completion_before_process_exit(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    db_conn.execute(
        "UPDATE schedules SET applied_revision = 1, status = 'completed' WHERE id = %s", (sid,)
    )
    db_conn.commit()
    backend.hold(session_name(f"schedule-{sid}"), _runner_command(sid, 1))
    with base.db.pool(gate=database_gate) as pool:
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
    assert backend.launches == 0
    assert backend.killed == []
    assert db_conn.execute("SELECT status FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "completed",
    )


def test_reap_failure_keeps_convergence_pending(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    backend.hold(session_name(f"schedule-{sid}"), _runner_command(sid, 0))
    backend.fail_reap = True
    with base.db.pool(gate=database_gate) as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW) == 0
    assert backend.launches == 0
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


def test_failed_revision_is_not_retried_after_unknown_launch(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    backend.fail_launch = True
    with base.db.pool(gate=database_gate) as pool:
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
    assert backend.launches == 1
    assert db_conn.execute(
        "SELECT launch_count, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (1, 1)


def test_an_uncertain_live_revision_is_never_blindly_replaced(
    db_conn: psycopg.Connection,
    backend: Backend,
    provenance: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    sid = _insert(db_conn)
    backend.hold(session_name(f"schedule-{sid}"), _runner_command(sid, 1))
    # meiva:note
    # the service no longer names the session: its provenance is unreadable
    provenance.clear()
    with base.db.pool(gate=database_gate) as pool:
        session_control.enqueue_blocking(pool, sid)
        assert requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW) == 0
    assert backend.launches == 0
    assert backend.killed == []
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]


@pytest.mark.parametrize("blocked_by", ["surviving-session", "unreadable-provenance"])
def test_a_blocked_request_is_retried_for_a_bounded_window_then_consumed_with_an_alert(
    db_conn: psycopg.Connection,
    backend: Backend,
    provenance: list[dict[str, Any]],
    blocked_by: str,
    emitted_events: EmittedEvents,
    *,
    database_gate: ProcessDbGate,
) -> None:
    sid = _insert(db_conn)
    name = session_name(f"schedule-{sid}")
    backend.hold(name, _runner_command(sid, 0))
    if blocked_by == "surviving-session":
        backend.fail_reap = True
    else:
        provenance.clear()
    emitted_events.clear()
    with base.db.pool(gate=database_gate) as pool:
        mgr = schedule_manager(pool, backend)
        session_control.enqueue_blocking(pool, sid)
        now = NOW
        while now < NOW + BLOCKED_GIVE_UP_AFTER_S:
            assert requests.consume_requests(pool, mgr, now=now) == 0
            now += 30
        assert emitted_events.names() == []
        assert requests.consume_requests(pool, mgr, now=now) == 1
        killed = list(backend.killed)
        for _ in range(3):
            now += 1
            assert requests.consume_requests(pool, mgr, now=now) == 0
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == []
    assert db_conn.execute("SELECT status FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "error",
    )
    assert emitted_events.names() == ["schedule_convergence_abandoned"]
    assert backend.killed == killed
    assert backend.launches == 0
    assert name in backend.commands


def test_a_failed_schedule_does_not_block_other_queued_revisions(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    backend.hold(session_name(f"schedule-{sid}"), "legacy")
    backend.fail_reap = True
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, desired_revision) VALUES ('other', 'pass', 'python schedule.py', 1) RETURNING id"
    ).fetchone()
    assert row is not None
    other = row[0]
    db_conn.commit()
    with base.db.pool(gate=database_gate) as pool:
        session_control.enqueue_blocking(pool, sid)
        session_control.enqueue_blocking(pool, other)
        assert requests.consume_requests(pool, schedule_manager(pool, backend), now=NOW) == 1
    assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [(sid,)]
    assert backend.launches == 1


def test_independent_managers_cannot_own_one_schedule_concurrently(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    sid = _insert(db_conn)
    started = Event()
    release = Event()

    def hold_the_launch() -> None:
        started.set()
        assert release.wait(5)

    backend.during_launch = hold_the_launch
    with (
        base.db.pool(max_size=4, gate=database_gate) as pool,
        ThreadPoolExecutor(max_workers=2) as workers,
    ):
        first = workers.submit(schedule_manager(pool, backend).sync_one, sid, now=NOW)
        try:
            assert started.wait(5)
            second = workers.submit(schedule_manager(pool, backend).sync_one, sid, now=NOW)
            assert second.result(timeout=3) is False
        finally:
            release.set()
        assert first.result(timeout=3) is True
    assert backend.launches == 1
    assert backend.killed == []


def test_newer_edit_during_launch_is_not_acknowledged_as_applied(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    from gateway.schedules import router

    sid = _insert(db_conn)
    with base.db.pool(max_size=4, gate=database_gate) as pool:

        def edit_during_launch() -> None:
            router.update_blocking(pool, sid, {"script": "print(2)"})

        backend.during_launch = edit_during_launch
        assert not schedule_manager(pool, backend).sync_one(sid, now=NOW)
        assert db_conn.execute(
            "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
        ).fetchone() == (2, 1)
        backend.during_launch = None
        assert schedule_manager(pool, backend).sync_one(sid, now=NOW)
    assert db_conn.execute(
        "SELECT desired_revision, applied_revision FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (2, 2)
    assert backend.launches == 2
    assert backend.killed == [session_name(f"schedule-{sid}")]


def test_old_reap_retry_cannot_kill_a_matching_applied_replacement(
    db_conn: psycopg.Connection, backend: Backend, *, database_gate: ProcessDbGate
) -> None:
    sid = _insert(db_conn)
    with base.db.pool(gate=database_gate) as pool:
        mgr = schedule_manager(pool, backend)
        assert mgr.sync_one(sid, now=NOW)
        mgr._reap_retries.add(sid)
        mgr.reconcile(now=NOW)
    assert backend.launches == 1
    assert backend.killed == []
