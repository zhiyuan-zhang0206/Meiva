from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from base.deploy.lifecycle import status_journal as journal


@pytest.fixture
def home(unit_home: Path) -> Path:
    return unit_home


# meiva:note
# The pid an entry hands the journal: a live process that is not this one, so a journal that read
# its own pid instead of the one it was given would record a different number.
_ENTRY_PID = os.getppid()


def test_begin_phase_finish_round_trip(home: Path) -> None:
    assert journal.begin(home, "stop", deadline=time.monotonic() + 30, pid=_ENTRY_PID) is True
    with journal.phase(home, "drain", pid=_ENTRY_PID):
        pass
    journal.finish(home, 0)
    op = journal.read(home)
    assert op is not None
    assert op.operation == "stop"
    assert op.pid == _ENTRY_PID
    assert op.boot_time == psutil.boot_time()
    assert op.complete is True
    assert op.result == {"rc": 0, "error": None}
    assert [p.name for p in op.phases] == ["drain"]
    assert op.phases[0].ok is True
    assert op.phases[0].finished_at is not None


def test_failed_phase_is_recorded_and_reraised(home: Path) -> None:
    journal.begin(home, "stop", pid=_ENTRY_PID)
    with (
        pytest.raises(RuntimeError, match="boom"),
        journal.phase(home, "services", pid=_ENTRY_PID),
    ):
        raise RuntimeError("boom")
    journal.finish(home, 1, error="RuntimeError: boom", extra={"phases": "services 1.2s"})
    op = journal.read(home)
    assert op is not None
    assert op.complete is True
    assert op.phases[0].ok is False
    assert op.phases[0].finished_at is not None
    assert op.result is not None and op.result["rc"] == 1
    error = op.result["error"]
    assert isinstance(error, str) and "boom" in error


# meiva:note
# A phase whose body reports failure is recorded as failed, not as ok: work that returned a
# failure code has not succeeded even though nothing raised.
def test_a_phase_whose_body_reports_failure_is_not_ok(home: Path) -> None:
    journal.begin(home, "restart", pid=_ENTRY_PID)
    with journal.phase(home, "start", pid=_ENTRY_PID) as span:
        span.ok = False
    op = journal.read(home)
    assert op is not None
    assert [phase.name for phase in op.phases] == ["start"]
    assert op.phases[0].ok is False


def test_inner_stop_leg_never_hijacks_the_restart_journal(home: Path) -> None:
    assert journal.begin(home, "restart", pid=_ENTRY_PID) is True
    with journal.phase(home, "preflight", pid=_ENTRY_PID):
        pass
    assert journal.begin(home, "stop", pid=_ENTRY_PID) is False
    with (
        journal.phase(home, "stop", pid=_ENTRY_PID),
        journal.phase(home, "drain", pid=_ENTRY_PID),
    ):
        pass
    op = journal.read(home)
    assert op is not None
    assert op.operation == "restart"
    assert op.complete is False
    assert [p.name for p in op.phases] == ["preflight", "stop", "drain"]


def test_completed_journal_can_be_replaced(home: Path) -> None:
    journal.begin(home, "stop", pid=_ENTRY_PID)
    journal.finish(home, 0)
    assert journal.begin(home, "restart", pid=_ENTRY_PID) is True
    op = journal.read(home)
    assert op is not None and op.operation == "restart" and op.complete is False
    assert op.pid == _ENTRY_PID


def test_malformed_journal_reads_as_absent(home: Path) -> None:
    path = journal.status_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": 5}))
    assert journal.read(home) is None


def test_wrong_shape_journal_reads_as_absent(home: Path) -> None:
    path = journal.status_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"operation": "stop"}]))
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": "stop"}))
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": "stop", "pid": "x"}))
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": "stop", "pid": 1, "started_at": []}))
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": "stop", "pid": 1, "deadline": "soon"}))
    assert journal.read(home) is None
    path.write_text(json.dumps({"operation": "stop", "pid": 1, "boot_time": "yesterday"}))
    assert journal.read(home) is None


def test_journal_survives_an_unfinished_process(home: Path) -> None:
    journal.begin(home, "restart", pid=_ENTRY_PID)
    with journal.phase(home, "stop", pid=_ENTRY_PID):
        pass
    with pytest.raises(SystemExit), journal.phase(home, "start", pid=_ENTRY_PID):
        raise SystemExit(1)
    op = journal.read(home)
    assert op is not None
    assert op.complete is False
    assert op.phases[-1].name == "start"
    assert op.phases[-1].ok is False


def test_phase_without_begin_still_journals(home: Path) -> None:
    with journal.phase(home, "mystery", pid=_ENTRY_PID):
        pass
    op = journal.read(home)
    assert op is not None
    assert op.operation == "unknown"
    assert op.pid == _ENTRY_PID
    assert [p.name for p in op.phases] == ["mystery"]


def _reaped_pid() -> int:
    child = subprocess.Popen([sys.executable, "-m", "base.deploy.lifecycle.tests.reaped_child"])
    assert child.wait(timeout=30) == 0
    return child.pid


def _op_with_pid(pid: int) -> journal.LifecycleOp:
    return journal.LifecycleOp(operation="stop", pid=pid, started_at=0.0, deadline=None)


def test_begin_takes_over_a_dead_writers_journal(home: Path) -> None:
    journal.begin(home, "restart", pid=_ENTRY_PID)
    with journal.phase(home, "stop", pid=_ENTRY_PID):
        pass
    raw = json.loads(journal.status_path(home).read_text())
    raw["pid"] = _reaped_pid()
    raw["boot_time"] = psutil.boot_time() - 86400
    journal.status_path(home).write_text(json.dumps(raw))

    assert journal.begin(home, "restart", pid=_ENTRY_PID) is True
    op = journal.read(home)
    assert op is not None
    assert op.operation == "restart" and op.complete is False
    assert op.pid == _ENTRY_PID != os.getpid()
    assert op.boot_time == psutil.boot_time()
    assert op.resumed_from is not None
    assert op.resumed_from.pid == raw["pid"]
    assert op.resumed_from.operation == "restart"
    assert [p.name for p in op.phases] == ["stop"]


def test_writer_alive_tracks_pid_state(home: Path) -> None:
    assert journal.writer_alive(_op_with_pid(os.getpid())) is True
    assert journal.writer_alive(_op_with_pid(_reaped_pid())) is False

    zombie = subprocess.Popen([sys.executable, "-m", "base.deploy.lifecycle.tests.reaped_child"])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(0.01)
        assert psutil.Process(zombie.pid).status() == psutil.STATUS_ZOMBIE
        assert journal.writer_alive(_op_with_pid(zombie.pid)) is False
    finally:
        zombie.wait(timeout=30)


def test_journal_write_failure_never_breaks_the_operation(
    home: Path,
    loguru_records: list[dict[str, object]],
) -> None:
    blocked = journal.journal_path(home)
    blocked.mkdir(parents=True)

    journal.begin(home, "stop", pid=_ENTRY_PID)
    with journal.phase(home, "drain", pid=_ENTRY_PID):
        pass
    journal.finish(home, 0)

    assert journal.read(home) is None
    assert any("journal write failed" in str(record["message"]) for record in loguru_records)


# meiva:note
# An operator's stop or restart is underway only while its journal is open, before the deadline it
# recorded, and its writer lives: no journal (root's own health replacement writes none), a finished
# one, one past its deadline (a writer killed mid-operation, whose pid may since have been reused),
# one with no deadline, or one whose writer is gone all read as not underway.
def test_operator_stop_in_progress_reads_an_open_live_stop_or_restart(home: Path) -> None:
    now = time.monotonic()
    assert journal.operator_stop_in_progress(home, now=now) is False
    for operation in ("stop", "restart"):
        journal.journal_path(home).unlink(missing_ok=True)
        journal.begin(home, operation, pid=_ENTRY_PID, deadline=now + 60)
        assert journal.operator_stop_in_progress(home, now=now) is True
        assert journal.operator_stop_in_progress(home, now=now + 61) is False
        journal.finish(home, 0)
        assert journal.operator_stop_in_progress(home, now=now) is False
    journal.journal_path(home).unlink()
    journal.begin(home, "stop", pid=_ENTRY_PID)
    assert journal.operator_stop_in_progress(home, now=now) is False
    journal.journal_path(home).unlink()
    journal.begin(home, "stop", pid=_reaped_pid(), deadline=now + 60)
    assert journal.operator_stop_in_progress(home, now=now) is False


@pytest.mark.parametrize("operation", ["stop", "restart"])
@pytest.mark.parametrize("boot_time", [None, psutil.boot_time() - 86400])
def test_operator_stop_rejects_a_previous_or_unknown_boot(
    home: Path, operation: str, boot_time: float | None
) -> None:
    now = time.monotonic()
    journal.begin(home, operation, pid=_ENTRY_PID, deadline=now + 86400)
    path = journal.journal_path(home)
    raw = json.loads(path.read_text())
    if boot_time is None:
        del raw["boot_time"]
    else:
        raw["boot_time"] = boot_time
    path.write_text(json.dumps(raw))
    op = journal.read(home)
    assert op is not None
    assert journal.writer_alive(op) is True
    assert op.deadline is not None and now < op.deadline
    assert journal.operator_stop_in_progress(home, now=now) is False


@pytest.mark.parametrize("offset", [-1.0, 1.0])
def test_operator_stop_tolerates_small_boot_timestamp_changes(home: Path, offset: float) -> None:
    now = time.monotonic()
    journal.begin(home, "stop", pid=_ENTRY_PID, deadline=now + 60)
    path = journal.journal_path(home)
    raw = json.loads(path.read_text())
    raw["boot_time"] = psutil.boot_time() + offset
    path.write_text(json.dumps(raw))
    assert journal.operator_stop_in_progress(home, now=now) is True
