# meiva:note
# The stall guard's thread lifecycle: it stops when closed and leaves no thread behind.
#
# Verdicts (`stall_policy`, `guard_admission`) are pure and tested with values; the frames the
# guard samples are tested against real threads in `gateway/tests/extensions/
# test_schedule_stall_guard.py`; the hard exit and the cleanup that follows it run in a real
# runner process (`tests/integration/test_schedule_runner_cleanup.py`).

from __future__ import annotations

import threading

import psycopg

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules.tests.runner_inputs import run_schedule as run
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.stall_policy import StallPolicy


def test_closing_a_guard_that_never_stalled_ends_its_thread(database_gate: ProcessDbGate) -> None:
    guard = engine.StallGuard(
        Database.from_settings(gate=database_gate), 1, None, policy=StallPolicy(timeout_s=3600.0)
    )

    guard.close()

    assert not guard.thread.is_alive()
    assert not guard.action_started
    assert guard.stop.is_set()


def test_a_finished_run_leaves_no_guard_thread(
    db_conn: psycopg.Connection, unit_home: object, *, machine_identity: MachineIdentityReader
) -> None:
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('guard-thread', 'pass', 'python schedule.py', true, 'stopped') RETURNING id"
    ).fetchone()
    db_conn.commit()
    assert row is not None

    assert run(row[0], machine=machine_identity) == 0

    assert not [t for t in threading.enumerate() if t.name.endswith("-stall-guard")]
