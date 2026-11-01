from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.schedules.tests.db_faults import failing_write
from gateway.schedules.tests.runner_inputs import run_schedule as run
from services.wake.schedule_manager.engine import script_filename
from services.wake.schedule_manager.stall_policy import StallPolicy


def _insert_schedule(
    conn: psycopg.Connection,
    *,
    script: str,
    command: str = "python schedule.py",
    enabled: bool = True,
    name: str = "s",
    machine: str = "gateway",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, machine) "
            "VALUES (%s, %s, %s, %s, 'stopped', %s) RETURNING id",
            (name, script, command, enabled, machine),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _status_and_error(conn: psycopg.Connection, sid: int) -> tuple[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def test_run_materializes_and_executes_and_binds_actor(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    marker = unit_home / "ran.txt"
    script = f"import meiva.sdk_surface.agent_identity, pathlib\npathlib.Path({str(marker)!r}).write_text(meiva.sdk_surface.agent_identity.require_actor())\n"
    sid = _insert_schedule(db_conn, script=script)

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    assert (unit_home / "schedules" / str(sid) / "schedule.py").read_text() == script
    assert marker.read_text() == f"schedule:{sid}"


def test_run_marks_completed_on_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None


def test_run_marks_completed_on_clean_subprocess_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="exit 0\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity)

    assert rc == 0
    status, _ = _status_and_error(db_conn, sid)
    assert status == "completed"


def test_run_captures_crash_to_last_error(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('boom-42')\n")

    rc = run(sid, machine=machine_identity)

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    assert last_error is not None and "boom-42" in last_error
    assert status != "completed"


def test_run_nonzero_subprocess_is_not_completed(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="exit 3\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity)

    assert rc == 3
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "exited 3" in last_error


def _runs(conn: psycopg.Connection, sid: int) -> list[tuple[bool | None, str | None]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s ORDER BY id",
            (sid,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def test_run_records_clean_py_exit(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1 + 1\n")

    assert run(sid, machine=machine_identity) == 0
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_crash(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('x' * 5000 + 'the-end')\n")

    assert run(sid, machine=machine_identity) == 1
    [(ok, note)] = _runs(db_conn, sid)
    assert ok is False and note is not None and len(note) <= 3100
    assert note.splitlines()[0] == "crashed: RuntimeError" and note.endswith("the-end")


def test_run_records_nonzero_command(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="exit 3\n", command="bash run.sh")

    assert run(sid, machine=machine_identity) == 3
    assert _runs(db_conn, sid) == [(False, "command exited 3")]


def test_run_records_py_sys_exit_zero(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="raise SystemExit(0)\n")

    assert run(sid, machine=machine_identity) == 0
    status, last_error = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert last_error is None
    assert _runs(db_conn, sid) == [(True, None)]


def test_run_records_py_sys_exit_nonzero(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="raise SystemExit(3)\n")

    assert run(sid, machine=machine_identity) == 3
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "script exited 3" in last_error
    assert _runs(db_conn, sid) == [(False, "script exited 3")]


def test_agent_status_guard_failure_is_visible_in_schedule_records(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    script = """
class DriftedAgentStatus:
    RUNNING = "running"

missing = sorted({"IDLING", "RUNNING"} - set(dir(DriftedAgentStatus)))
if missing:
    raise SystemExit(
        "schedule 'drift-probe' is missing AgentStatus members: " + ", ".join(missing)
    )
"""
    sid = _insert_schedule(db_conn, script=script, name="drift-probe")

    assert run(sid, machine=machine_identity) == 1
    status, last_error = _status_and_error(db_conn, sid)
    expected = "schedule 'drift-probe' is missing AgentStatus members: IDLING"
    assert status != "completed"
    assert last_error is not None and expected in last_error
    assert _runs(db_conn, sid) == [(False, f"script exited 1: {expected}")]


@pytest.mark.parametrize(
    ("exit_expr", "expected_note"),
    [
        ("'done early'", "script exited 1: done early"),
        ("3.7", "script exited 1: 3.7"),
    ],
)
def test_run_records_py_sys_exit_message(
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_expr: str,
    expected_note: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    sid = _insert_schedule(db_conn, script=f"raise SystemExit({exit_expr})\n")

    assert run(sid, machine=machine_identity) == 1
    assert _runs(db_conn, sid) == [(False, expected_note)]


@pytest.mark.parametrize(
    ("exit_expr", "expected_rc", "expected_rows"),
    [
        ("True", 1, [(False, "script exited 1")]),
        ("False", 0, [(True, None)]),
    ],
)
def test_run_records_py_sys_exit_bool(
    db_conn: psycopg.Connection,
    unit_home: Path,
    exit_expr: str,
    expected_rc: int,
    expected_rows: list[tuple[bool | None, str | None]],
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    sid = _insert_schedule(db_conn, script=f"raise SystemExit({exit_expr})\n")

    assert run(sid, machine=machine_identity) == expected_rc
    assert _runs(db_conn, sid) == expected_rows


def test_run_records_stall_timeout(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="sleep 60\n", command="bash run.sh")

    assert run(sid, machine=machine_identity, policy=StallPolicy(timeout_s=1.0)) == 1
    assert _runs(db_conn, sid) == [(False, "stall timeout (1s)")]


def test_run_skips_disabled_schedule(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="raise RuntimeError('should not run')\n", enabled=False)
    assert run(sid, machine=machine_identity) == 0
    assert not (unit_home / "schedules" / str(sid)).exists()
    with db_conn.cursor() as cur:
        cur.execute("SELECT last_error FROM schedules WHERE id = %s", (sid,))
        row = cur.fetchone()
    assert row is not None and row[0] is None
    assert _runs(db_conn, sid) == []


def test_run_hands_py_script_a_clean_argv(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    runner_argv = sys.argv
    marker = unit_home / "argv.json"
    script = (
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(sys.argv))\n"
    )
    sid = _insert_schedule(db_conn, script=script)

    assert run(sid, machine=machine_identity) == 0
    assert json.loads(marker.read_text()) == [
        str(unit_home / "schedules" / str(sid) / "schedule.py")
    ]
    assert sys.argv is runner_argv


def test_run_record_failure_does_not_break_the_run(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1\n")

    with failing_write(db_conn, table="schedule_runs", events="INSERT OR UPDATE"):
        assert run(sid, machine=machine_identity) == 0

    status, _ = _status_and_error(db_conn, sid)
    assert status == "completed"
    assert _runs(db_conn, sid) == []


def test_run_completed_marker_failure_keeps_run_row_honest(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="x = 1\n")

    with failing_write(
        db_conn, table="schedules", events="UPDATE", when="NEW.status = 'completed'"
    ):
        assert run(sid, machine=machine_identity) == 0

    status, _ = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert _runs(db_conn, sid) == [(True, "completed-marker write failed")]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python schedule.py", "schedule.py"),
        ("python3 -u run_me.py", "run_me.py"),
        ("bash run.sh", "run.sh"),
        ("some-binary --flag", "schedule.py"),
        ("python3.11 main.py", "main.py"),
        ("python3.12 -u run_me.py", "run_me.py"),
        ("bash -l run.sh", "run.sh"),
        ("some-binary --config=v1.2 run.py", "run.py"),
    ],
)
def test_script_filename(command: str, expected: str) -> None:
    assert script_filename(command) == expected


def test_run_hung_subprocess_times_out_and_records_error(
    db_conn: psycopg.Connection, unit_home: Path, *, machine_identity: MachineIdentityReader
) -> None:
    sid = _insert_schedule(db_conn, script="sleep 60\n", command="bash run.sh")

    rc = run(sid, machine=machine_identity, policy=StallPolicy(timeout_s=1.0))

    assert rc == 1
    status, last_error = _status_and_error(db_conn, sid)
    assert status != "completed"
    assert last_error is not None and "did not finish within 1s" in last_error
