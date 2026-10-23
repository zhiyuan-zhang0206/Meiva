# meiva:note
# The stall guard's thread lifecycle: how it ends, and what a failure inside it does.
#
# Verdicts (`stall_policy`, `guard_admission`) are pure and tested with values; the hard exit and
# the cleanup that follows it run in a real runner process
# (`tests/integration/test_schedule_runner_cleanup.py`). What remains here is the thread: it stops
# when closed, and a failure inside it is never silent. No real resource makes the frame reader
# fail, so `_FailingGuard` is the one place a test supplies a fault, by overriding the sampling
# method of the guard under test.

from __future__ import annotations

import threading
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from gateway.schedules.tests.runner_inputs import run_schedule as run
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.stall_policy import Signature, StallPolicy

_QUICK = StallPolicy(timeout_s=0.02)


def _log_text(records: list[dict[str, Any]]) -> str:
    return "\n".join(str(record["message"]) for record in records)


class WorkerFailure(BaseException):
    pass


# meiva:note
# A guard whose frame reader fails once it is entered, optionally after being released.
class _FailingGuard(engine.StallGuard):
    def __init__(
        self, database: Database, fault: BaseException, *, release: threading.Event | None = None
    ) -> None:
        self.fault = fault
        self.release = release
        self.entered = threading.Event()
        super().__init__(database, 1, None, policy=_QUICK)

    def sample(self) -> Signature | None:
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(2)
        raise self.fault


def test_closing_a_guard_that_never_stalled_ends_its_thread(database_gate: ProcessDbGate) -> None:
    guard = engine.StallGuard(
        Database.from_settings(gate=database_gate), 1, None, policy=StallPolicy(timeout_s=3600.0)
    )

    guard.close()

    assert not guard.thread.is_alive()
    assert not guard.action_started
    assert guard.stop.is_set()


def test_a_finished_run_leaves_no_guard_thread(
    db_conn: psycopg.Connection, unit_home: object, *, machine_identity: MachineIdentity
) -> None:
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('guard-thread', 'pass', 'python schedule.py', true, 'stopped') RETURNING id"
    ).fetchone()
    db_conn.commit()
    assert row is not None

    assert run(row[0], machine=machine_identity) == 0

    assert not [t for t in threading.enumerate() if t.name.endswith("-stall-guard")]


def test_guard_unknown_error_is_visible_and_rethrown(
    loguru_records: list[dict[str, Any]], database_gate: ProcessDbGate
) -> None:
    error = WorkerFailure("frame reader implementation failed")
    guard = _FailingGuard(Database.from_settings(gate=database_gate), error)
    assert guard.entered.wait(2)

    with pytest.raises(WorkerFailure) as caught:
        guard.close()

    assert caught.value is error
    assert guard.error is error
    assert not guard.thread.is_alive()
    assert "stall guard failed" in _log_text(loguru_records)


def test_blocked_guard_close_is_finite_and_collects_its_late_error(
    loguru_records: list[dict[str, Any]], database_gate: ProcessDbGate
) -> None:
    release = threading.Event()
    error = WorkerFailure("late observer failure")
    guard = _FailingGuard(Database.from_settings(gate=database_gate), error, release=release)
    try:
        assert guard.entered.wait(2)
        with pytest.raises(RuntimeError, match="stall guard did not stop"):
            guard.close(timeout=0.01)
        assert guard.thread.is_alive()
    finally:
        release.set()
        guard.thread.join(timeout=2)

    assert "stall guard failed" in _log_text(loguru_records)
    with pytest.raises(WorkerFailure) as caught:
        guard.close()
    assert caught.value is error
    assert not guard.thread.is_alive()
