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


def test_begin_phase_finish_round_trip(home: Path) -> None:
    assert journal.begin(home, "stop", deadline=time.monotonic() + 30, pid=os.getpid()) is True
    with journal.phase(home, "drain", pid=os.getpid()):
        pass
    journal.finish(home, 0)
    op = journal.read(home)
    assert op is not None
    assert op.operation == "stop"
    assert op.complete is True
    assert op.result == {"rc": 0, "error": None}
    assert [p.name for p in op.phases] == ["drain"]
    assert op.phases[0].ok is True
    assert op.phases[0].finished_at is not None


def test_failed_phase_is_recorded_and_reraised(home: Path) -> None:
    journal.begin(home, "stop", pid=os.getpid())
    with (
        pytest.raises(RuntimeError, match="boom"),
        journal.phase(home, "services", pid=os.getpid()),
    ):
        raise RuntimeError("boom")
    journal.finish(home, 1, error="RuntimeError: boom", extra={"phases": "services 1.2s"})
    op = journal.read(home)
    assert op is not None
    assert op.complete is True
    assert op.phases[0].ok is False
    assert op.phases[0].finished_at is not None
    assert op.result is not None and op.result["rc"] == 1
    assert "boom" in op.result["error"]


def test_inner_stop_leg_never_hijacks_the_restart_journal(home: Path) -> None:
    assert journal.begin(home, "restart", pid=os.getpid()) is True
    with journal.phase(home, "preflight", pid=os.getpid()):
        pass
    assert journal.begin(home, "stop", pid=os.getpid()) is False
    with (
        journal.phase(home, "stop", pid=os.getpid()),
        journal.phase(home, "drain", pid=os.getpid()),
    ):
        pass
    op = journal.read(home)
    assert op is not None
    assert op.operation == "restart"
    assert op.complete is False
    assert [p.name for p in op.phases] == ["preflight", "stop", "drain"]


def test_completed_journal_can_be_replaced(home: Path) -> None:
    journal.begin(home, "stop", pid=os.getpid())
    journal.finish(home, 0)
    assert journal.begin(home, "restart", pid=os.getpid()) is True
    op = journal.read(home)
    assert op is not None and op.operation == "restart" and op.complete is False


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


def test_journal_survives_an_unfinished_process(home: Path) -> None:
    journal.begin(home, "restart", pid=os.getpid())
    with journal.phase(home, "stop", pid=os.getpid()):
        pass
    with pytest.raises(SystemExit), journal.phase(home, "start", pid=os.getpid()):
        raise SystemExit(1)
    op = journal.read(home)
    assert op is not None
    assert op.complete is False
    assert op.phases[-1].name == "start"
    assert op.phases[-1].ok is False


def test_phase_without_begin_still_journals(home: Path) -> None:
    with journal.phase(home, "mystery", pid=os.getpid()):
        pass
    op = journal.read(home)
    assert op is not None
    assert op.operation == "unknown"
    assert [p.name for p in op.phases] == ["mystery"]


def _reaped_pid() -> int:
    child = subprocess.Popen([sys.executable, "-m", "base.deploy.lifecycle.tests.reaped_child"])
    assert child.wait(timeout=30) == 0
    return child.pid


def _op_with_pid(pid: int) -> journal.LifecycleOp:
    return journal.LifecycleOp(operation="stop", pid=pid, started_at=0.0, deadline=None)


def test_begin_takes_over_a_dead_writers_journal(home: Path) -> None:
    journal.begin(home, "restart", pid=os.getpid())
    with journal.phase(home, "stop", pid=os.getpid()):
        pass
    raw = json.loads(journal.status_path(home).read_text())
    raw["pid"] = _reaped_pid()
    journal.status_path(home).write_text(json.dumps(raw))

    assert journal.begin(home, "restart", pid=os.getpid()) is True
    op = journal.read(home)
    assert op is not None
    assert op.operation == "restart" and op.complete is False
    assert op.pid == os.getpid()
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

    journal.begin(home, "stop", pid=os.getpid())
    with journal.phase(home, "drain", pid=os.getpid()):
        pass
    journal.finish(home, 0)

    assert journal.read(home) is None
    assert any("journal write failed" in str(record["message"]) for record in loguru_records)
