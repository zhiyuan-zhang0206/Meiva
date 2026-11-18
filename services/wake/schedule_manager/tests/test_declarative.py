from __future__ import annotations

import json
import os
import signal
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeGuard

import psutil
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.clock.tests.fakes import clock_at
from base.config import settings
from base.db import create_agent
from base.host.boundary.untyped import object_of
from base.native_process.ownership import LiveReading, OwnedProcess
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL, ProcessControl
from base.native_process.tests.fake_process_control import FakeProcessControl
from services.wake.schedule_manager.declarative import (
    DeclarativeScheduleEngine,
    schedule_directory,
)
from services.wake.schedule_manager.manager import REPO_ROOT
from services.wake.schedule_manager.run_custody import (
    SYSTEM_PROCESS_IDENTITY_CONTROL,
    GroupLiveness,
    ProcessIdentityControl,
    RunClaim,
    kill_run_group,
    read_run_claim,
)


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool[psycopg.Connection]]:
    del db_url
    active: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2, open=True
    )
    try:
        yield active
    finally:
        active.close()


def _agent(db_conn: psycopg.Connection, machine: str = "gateway") -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s)",
        (agent_id, machine),
    )
    db_conn.commit()
    return agent_id


def _schedule(
    db_conn: psycopg.Connection,
    *,
    name: str,
    script: str,
    now: datetime,
    trigger_kind: str = "at",
    owner_agent_id: int | None = None,
    timeout_seconds: int = 10,
) -> int:
    run_at = now - timedelta(minutes=1) if trigger_kind == "at" else None
    cron_expr = "* * * * *" if trigger_kind == "cron" else None
    interval_seconds = 60 if trigger_kind == "every" else None
    row = db_conn.execute(
        "INSERT INTO schedules "
        "(name, script, command, enabled, status, machine, owner_agent_id, trigger_kind, "
        "run_at, cron_expr, interval_seconds, timezone, timeout_seconds, on_owner_terminate, "
        "next_launch_at) "
        "VALUES (%s, %s, 'python run.py', true, 'scheduled', 'gateway', %s, %s, %s, %s, %s, "
        "'UTC', %s, %s, %s) RETURNING id",
        (
            name,
            script,
            owner_agent_id,
            trigger_kind,
            run_at,
            cron_expr,
            interval_seconds,
            timeout_seconds,
            "cancel" if owner_agent_id is not None else "keep",
            now - timedelta(seconds=1),
        ),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    return row[0]


def _engine(
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
    now: datetime,
    *,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
    identity_control: ProcessIdentityControl = SYSTEM_PROCESS_IDENTITY_CONTROL,
) -> DeclarativeScheduleEngine:
    launch_env = dict(os.environ)
    launch_env["PYTHONPATH"] = str(REPO_ROOT)
    return DeclarativeScheduleEngine(
        pool,
        home=unit_home,
        repo_root=REPO_ROOT,
        machine_name="gateway",
        launch_env=launch_env,
        clock=clock_at(now),
        process_control=process_control,
        identity_control=identity_control,
        secret_env_resolver=None,
    )


class _IdentityControl:
    def __init__(self, readings: dict[OwnedProcess, LiveReading | None]) -> None:
        self._readings = readings
        self.signals: list[tuple[OwnedProcess, int]] = []

    def read_live(self, identity: OwnedProcess) -> LiveReading | None:
        return self._readings.get(identity)

    def signal_pinned(self, identity: OwnedProcess, signum: int) -> bool:
        self.signals.append((identity, signum))
        return self._readings.get(identity) is not None


def _wait_for_status(
    engine: DeclarativeScheduleEngine,
    db_conn: psycopg.Connection,
    schedule_id: int,
    wanted: str,
) -> tuple[object, ...]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        engine.reconcile()
        row = db_conn.execute(
            "SELECT status, enabled, next_launch_at, last_error FROM schedules WHERE id = %s",
            (schedule_id,),
        ).fetchone()
        if row is not None and row[0] == wanted:
            return row
        time.sleep(0.02)
    raise AssertionError(f"schedule {schedule_id} did not reach {wanted}")


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not created")


def _pid_live(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() not in (
            psutil.STATUS_DEAD,
            psutil.STATUS_ZOMBIE,
        )
    except psutil.Error:
        return False


def _wait_for_pid_exit(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _pid_live(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} did not exit")


def _kill_if_live(pid: int) -> None:
    if _pid_live(pid):
        os.kill(pid, signal.SIGKILL)


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _read_process_ids(path: Path) -> tuple[int, int]:
    raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    if not _is_object_dict(raw):
        raise AssertionError(f"{path} did not contain a process object")
    payload = raw.get("payload")
    supervisor = raw.get("supervisor")
    if type(payload) is not int or type(supervisor) is not int:
        raise AssertionError(f"{path} did not contain integer process ids")
    return payload, supervisor


def test_at_runs_once_in_owner_workspace_and_completes(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    owner = _agent(db_conn)
    sid = _schedule(
        db_conn,
        name="owned-at",
        now=now,
        owner_agent_id=owner,
        script=(
            "import os\n"
            "from pathlib import Path\n"
            "Path('state.json').write_text(os.getcwd())\n"
            "Path('scheduled.txt').write_text(os.environ['MEIVA_SCHEDULED_FOR'])\n"
            "print('done')\n"
        ),
    )
    engine = _engine(pool, unit_home, now)

    status = _wait_for_status(engine, db_conn, sid, "completed")

    work_dir = unit_home / "workspaces" / str(owner) / "schedules" / str(sid)
    assert status[1:] == (False, None, None)
    assert (work_dir / "run.py").exists()
    assert (work_dir / "state.json").read_text() == str(work_dir)
    assert "done" in (work_dir / "latest.log").read_text()
    runs = db_conn.execute(
        "SELECT ok, agent_id, scheduled_for FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchall()
    assert len(runs) == 1
    assert runs[0][0:2] == (True, owner)
    assert runs[0][2] is not None
    assert (work_dir / "scheduled.txt").read_text() == runs[0][2].isoformat()


def test_pending_legacy_sync_does_not_block_declarative_launch(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="await-legacy-reap",
        now=now,
        trigger_kind="every",
        script="from pathlib import Path\nPath('fired').write_text('yes')\n",
    )
    db_conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, 'gateway')",
        (sid,),
    )
    db_conn.commit()
    engine = _engine(pool, unit_home, now)
    work_dir = unit_home / "schedules" / str(sid)

    engine.reconcile()

    _wait_for_file(work_dir / "fired")
    assert db_conn.execute(
        "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_at_failure_is_terminal_without_retry(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="failed-at",
        now=now,
        script="raise RuntimeError('broken')\n",
    )
    engine = _engine(pool, unit_home, now)

    status = _wait_for_status(engine, db_conn, sid, "error")
    engine.reconcile()

    assert status[1] is False
    assert status[2] is None
    assert "process exited" in str(status[3])
    assert db_conn.execute(
        "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_every_advances_after_completion_without_catch_up_storm(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="recurring",
        now=now,
        trigger_kind="every",
        script="print('round')\n",
    )
    engine = _engine(pool, unit_home, now)

    status = _wait_for_status(engine, db_conn, sid, "scheduled")
    engine.reconcile()

    assert status[1] is True
    next_launch = status[2]
    assert isinstance(next_launch, datetime)
    assert next_launch > now
    assert db_conn.execute(
        "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_cron_advances_from_the_completed_slot_without_replaying_missed_slots(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="cron-recurring",
        now=now,
        trigger_kind="cron",
        script="print('cron round')\n",
    )
    engine = _engine(pool, unit_home, now)

    status = _wait_for_status(engine, db_conn, sid, "scheduled")
    engine.reconcile()

    next_launch = status[2]
    assert isinstance(next_launch, datetime)
    assert next_launch > now
    assert next_launch.second == 0
    assert db_conn.execute(
        "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_timeout_kills_payload_and_records_error(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="timeout-at",
        now=now,
        timeout_seconds=1,
        script="import time\ntime.sleep(30)\n",
    )
    engine = _engine(pool, unit_home, now)

    status = _wait_for_status(engine, db_conn, sid, "error")

    assert status[1] is False
    run = db_conn.execute(
        "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone()
    assert run is not None
    assert run[0] is False
    assert "timed out after 1s" in run[1]


def test_supervisor_sigkill_keeps_run_open_until_its_payload_group_is_gone(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="supervisor-killed",
        now=now,
        trigger_kind="every",
        timeout_seconds=20,
        script=(
            "import json, os, time\n"
            "from pathlib import Path\n"
            "Path('processes.json').write_text(json.dumps("
            "{'payload': os.getpid(), 'supervisor': os.getppid()}))\n"
            "time.sleep(30)\n"
        ),
    )
    engine = _engine(pool, unit_home, now)
    engine.reconcile()
    work_dir = schedule_directory(unit_home, None, sid)
    marker = work_dir / "processes.json"
    _wait_for_file(marker)
    payload_pid, supervisor_pid = _read_process_ids(marker)
    assert payload_pid != supervisor_pid
    run_id_row = db_conn.execute(
        "SELECT id FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone()
    assert run_id_row is not None
    claim_reading = read_run_claim(work_dir, sid, run_id_row[0])
    assert claim_reading.claim is not None
    assert claim_reading.claim.member is not None
    assert claim_reading.claim.member.pid == payload_pid

    os.kill(supervisor_pid, signal.SIGKILL)
    try:
        _wait_for_pid_exit(supervisor_pid)
        unreadable_control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
        unreadable_control.refuse(supervisor_pid, PermissionError("process table denied"))
        _engine(
            pool,
            unit_home,
            now,
            process_control=unreadable_control,
        ).reconcile()
        assert db_conn.execute(
            "SELECT ok FROM schedule_runs WHERE schedule_id = %s", (sid,)
        ).fetchone() == (None,)
        sent = {(item.kind, item.target, item.signum) for item in unreadable_control.signals}
        assert ("killpg", supervisor_pid, signal.SIGKILL) in sent
        assert _pid_live(payload_pid)

        _wait_for_status(engine, db_conn, sid, "error")
        run = db_conn.execute(
            "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s", (sid,)
        ).fetchone()
        assert run is not None
        assert run[0] is False
        assert not _pid_live(payload_pid)
        assert db_conn.execute(
            "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
        ).fetchone() == (1,)
    finally:
        _kill_if_live(payload_pid)


def test_a_reused_numeric_group_without_a_live_captured_member_is_never_killed() -> None:
    leader = OwnedProcess(pid=4100, birth=1.0, starttime=10)
    member = OwnedProcess(pid=4101, birth=2.0, starttime=11)
    claim = RunClaim(
        schedule_id=1,
        run_id=2,
        leader=leader,
        pgid=leader.pid,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        member=member,
    )
    identities = _IdentityControl({leader: None, member: None})
    # meiva:note
    # A later generation reused the numeric PGID and its own leader already exited; only its
    # foreign members remain, so the current observations are two absent births plus a group.
    processes = FakeProcessControl()

    observed = kill_run_group(
        claim,
        process_control=processes,
        identity_control=identities,
    )

    assert observed.group is GroupLiveness.UNKNOWN
    assert "without a live captured run member" in observed.detail
    assert identities.signals == []
    assert [(sent.target, sent.signum) for sent in processes.signals] == [(leader.pid, 0)]


def test_open_run_prevents_overlapping_admission(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(db_conn, name="single-flight", now=now, script="pass\n")
    db_conn.execute(
        "INSERT INTO schedule_runs (schedule_id, scheduled_for) VALUES (%s, %s)", (sid, now)
    )
    db_conn.commit()

    _engine(pool, unit_home, now).reconcile()

    assert db_conn.execute(
        "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_workspace_locality_is_explicit_for_system_and_owned_schedules(tmp_path: Path) -> None:
    assert schedule_directory(tmp_path, None, 7) == tmp_path / "schedules" / "7"
    assert schedule_directory(tmp_path, 42, 7) == (
        tmp_path / "workspaces" / "42" / "schedules" / "7"
    )


def test_owner_termination_cancels_only_cancel_policy(
    db_conn: psycopg.Connection,
) -> None:
    now = datetime.now(UTC)
    owner = _agent(db_conn)
    cancelled = _schedule(
        db_conn, name="cancel-with-owner", now=now, script="pass\n", owner_agent_id=owner
    )
    kept = _schedule(db_conn, name="keep-with-owner", now=now, script="pass\n")
    db_conn.execute(
        "UPDATE schedules SET owner_agent_id = %s, on_owner_terminate = 'keep' WHERE id = %s",
        (owner, kept),
    )
    db_conn.execute(
        "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' WHERE id = %s",
        (owner,),
    )
    db_conn.commit()

    assert db_conn.execute(
        "SELECT enabled, status, next_launch_at FROM schedules WHERE id = %s", (cancelled,)
    ).fetchone() == (False, "cancelled", None)
    kept_row = db_conn.execute(
        "SELECT enabled, status, next_launch_at FROM schedules WHERE id = %s", (kept,)
    ).fetchone()
    assert kept_row is not None
    assert kept_row[0:2] == (True, "scheduled")
    assert kept_row[2] is not None


def test_owner_termination_stops_an_active_declarative_fire(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    owner = _agent(db_conn)
    sid = _schedule(
        db_conn,
        name="cancel-active-owner-run",
        now=now,
        owner_agent_id=owner,
        script="import time\ntime.sleep(30)\n",
    )
    engine = _engine(pool, unit_home, now)
    engine.reconcile()
    db_conn.execute(
        "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' WHERE id = %s",
        (owner,),
    )
    db_conn.commit()

    deadline = time.monotonic() + 10
    run = None
    while time.monotonic() < deadline:
        engine.reconcile()
        run = db_conn.execute(
            "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s", (sid,)
        ).fetchone()
        if run is not None and run[0] is False:
            break
        time.sleep(0.02)

    assert db_conn.execute(
        "SELECT enabled, status, next_launch_at FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (False, "cancelled", None)
    assert run == (False, "cancelled")


def test_argparse_payload_succeeds_without_runner_arguments(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
) -> None:
    now = datetime.now(UTC)
    sid = _schedule(
        db_conn,
        name="argparse-at",
        now=now,
        script=(
            "import argparse\n"
            "from pathlib import Path\n"
            "argparse.ArgumentParser().parse_args()\n"
            "Path('parsed.txt').write_text('ok')\n"
        ),
    )
    engine = _engine(pool, unit_home, now)
    _wait_for_status(engine, db_conn, sid, "completed")
    assert db_conn.execute(
        "SELECT ok FROM schedule_runs WHERE schedule_id = %s", (sid,)
    ).fetchall() == [(True,)]
    assert (schedule_directory(unit_home, None, sid) / "parsed.txt").read_text() == "ok"
