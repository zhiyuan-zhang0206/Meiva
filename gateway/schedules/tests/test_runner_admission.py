import psycopg

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.http.db_rows import query_one
from gateway.schedules.rows import InsertedId
from gateway.schedules.tests.runner_inputs import run_schedule as run


def test_stale_runner_revision_cannot_execute_newer_script(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    row = query_one(
        db_conn,
        InsertedId,
        "INSERT INTO schedules (name, script, command, desired_revision, machine) VALUES ('stale', 'raise RuntimeError()', 'python schedule.py', 2, 'gateway') RETURNING id",
    )
    assert row is not None
    db_conn.commit()
    assert run(row.id, revision=1, machine=machine_identity) == 0
    assert db_conn.execute(
        "SELECT status, applied_revision, last_error FROM schedules WHERE id = %s", (row.id,)
    ).fetchone() == ("stopped", 0, None)
    assert db_conn.execute("SELECT count(*) FROM schedule_runs").fetchone() == (0,)


def test_current_revision_is_applied_before_clean_completion(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    row = query_one(
        db_conn,
        InsertedId,
        "INSERT INTO schedules (name, script, command, desired_revision, machine) VALUES ('current', 'pass', 'python schedule.py', 2, 'gateway') RETURNING id",
    )
    assert row is not None
    db_conn.commit()
    assert run(row.id, revision=2, machine=machine_identity) == 0
    assert db_conn.execute(
        "SELECT status, applied_revision FROM schedules WHERE id = %s", (row.id,)
    ).fetchone() == ("completed", 2)
