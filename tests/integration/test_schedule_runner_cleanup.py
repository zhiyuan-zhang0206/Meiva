# meiva:note
# Real runner exits and externally observed child ownership boundaries.
#
# A runner is a real process on a real schedule row. Its failures are made in the database, where
# they happen in production: a write that cannot proceed is a write behind a row lock the test holds.

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal, LiteralString

import psutil
import psycopg
import pytest

_CHILD = Path(__file__).with_name("child_schedule_runner.py")


def _await_file(path: Path) -> None:
    deadline = time.monotonic() + 15
    while not path.exists():
        assert time.monotonic() < deadline, f"runner did not create {path}"
        time.sleep(0.02)


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() not in (
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        )
    except psutil.NoSuchProcess:
        return False


@contextmanager
def _processes(root: Path) -> Generator[list[psutil.Process]]:
    """Own every test process, including deliberately exempt double-fork daemons."""
    tracked: list[psutil.Process] = []
    try:
        yield tracked
    finally:
        # PID files also collect children if setup failed before the readiness barrier.
        for path in root.glob("*.pid"):
            with suppress(psutil.NoSuchProcess):
                process = psutil.Process(int(path.read_text()))
                if str(root) in " ".join(process.cmdline()):
                    tracked.append(process)
        for process in reversed(tracked):
            with suppress(psutil.NoSuchProcess):
                process.kill()
        psutil.wait_procs(tracked, timeout=3)


# meiva:note
# A row lock held from another connection, so a write to that row really blocks.
#
# FOR NO KEY UPDATE conflicts with an UPDATE of the row but not with the key-share lock a
# foreign-key check takes, so the runner's other writes proceed.
class _RowLock:
    def __init__(self, db_url: str, table: LiteralString, row_id: int) -> None:
        self._holder = psycopg.connect(db_url)
        # meiva:note
        # The SQL identifier is a fixed test constant, never input.
        self._holder.execute(f"SELECT 1 FROM {table} WHERE id = %s FOR NO KEY UPDATE", (row_id,))  # noqa: S608

    def release(self) -> None:
        self._holder.rollback()
        self._holder.close()


@contextmanager
def _locked(db_url: str, table: LiteralString, row_id: int) -> Generator[_RowLock]:
    lock = _RowLock(db_url, table, row_id)
    try:
        yield lock
    finally:
        with suppress(psycopg.OperationalError):
            lock.release()


# meiva:note
# Wait until some backend is stuck behind a lock on an UPDATE of `table`.
def _await_blocked_write(conn: psycopg.Connection, table: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        blocked = conn.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND query ILIKE %s",
            (f"UPDATE {table}%",),
        ).fetchone()
        conn.rollback()
        if blocked is not None and blocked[0] > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"no write to {table} ever blocked")


def _scripts(root: Path, *, complete: bool) -> str:
    worker = root / "worker.py"
    worker.write_text(
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "root, name = Path(sys.argv[1]), sys.argv[2]\n"
        "def term(*_):\n"
        "    (root / (name + '.term')).touch()\n"
        "    if name == 'child':\n"
        "        raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        "if name == 'child':\n"
        "    child = subprocess.Popen([sys.executable, __file__, str(root), 'grandchild'])\n"
        "elif name == 'daemon':\n"
        "    if os.fork():\n"
        "        raise SystemExit(0)\n"
        "    os.setsid()\n"
        "    if os.fork():\n"
        "        raise SystemExit(0)\n"
        "(root / (name + '.tmp')).write_text(str(os.getpid()))\n"
        "(root / (name + '.tmp')).replace(root / (name + '.pid'))\n"
        "time.sleep(60)\n"
    )
    return (
        "import os, socket, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"root = Path({str(root)!r})\n"
        f"worker = {str(worker)!r}\n"
        # Retain both live Popen objects: subprocess._active cannot discover these.
        "child = subprocess.Popen([sys.executable, worker, str(root), 'child'])\n"
        "session_child = subprocess.Popen(\n"
        "    [sys.executable, worker, str(root), 'session'], start_new_session=True)\n"
        "subprocess.run([sys.executable, worker, str(root), 'daemon'], check=True, timeout=5)\n"
        "while not (root / 'release').exists():\n"
        "    time.sleep(0.02)\n"
        # Both sleep and child wait outlast the stall budget, then leave a stable stall.
        "time.sleep(0.4)\n"
        "try:\n"
        "    child.communicate(timeout=0.4)\n"
        "except subprocess.TimeoutExpired:\n"
        "    pass\n"
        "(root / 'park-finished').touch()\n"
        + ("" if complete else "reader, writer = socket.socketpair()\nreader.recv(1)\n")
    )


def _capture_children(root: Path, runner_pid: int) -> list[psutil.Process]:
    owned: list[psutil.Process] = []
    for name in ("child", "grandchild", "session", "daemon"):
        path = root / f"{name}.pid"
        _await_file(path)
        owned.append(psutil.Process(int(path.read_text())))
    assert os.getsid(owned[2].pid) == owned[2].pid
    # Wait until the double-forked process really escaped the ancestry walk.
    deadline = time.monotonic() + 5
    while runner_pid in [parent.pid for parent in owned[3].parents()]:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return owned


def _assert_reaped(root: Path, owned: list[psutil.Process]) -> None:
    child, grandchild, session_child, daemon = owned
    assert _alive(daemon), "pre-capture reparented daemon must remain exempt"
    assert not _alive(child), "owned live-held Popen survived the runner hard exit"
    assert not _alive(grandchild), "TERM-resistant descendant survived escalation"
    assert not _alive(session_child), "setsid child escaped owned-child cleanup"
    assert all((root / f"{name}.term").exists() for name in ("child", "grandchild", "session"))


def _assert_untouched(root: Path, owned: list[psutil.Process]) -> None:
    child, grandchild, session_child, daemon = owned
    assert _alive(daemon), "pre-capture reparented daemon must remain exempt"
    assert all(_alive(process) for process in (child, grandchild, session_child))
    assert not list(root.glob("*.term")), "clean completion must not run stall cleanup"


# meiva:note
# A stalled runner reaps what it owns before its failure write, and a finished one reaps
# nothing. `blocked-record` holds the failure write behind a row lock until the test has seen the
# descendants gone; `blocked-completion` holds the completion write for longer than the stall
# budget, which a guard that outlived its script would punish with a kill.
@pytest.mark.parametrize(
    "mode", ["stall", "complete", "missing-module-files", "blocked-record", "blocked-completion"]
)
def test_runner_hard_exit_child_ownership(
    db_conn: psycopg.Connection, db_url: str, unit_home: Path, mode: str
) -> None:
    # The runner is a separate process that boots its config from its home's `.env`
    # (`MEIVA_HOME` is this test's `unit_home`, which starts bare): name the test
    # database there, as a started cluster's `.env` does.
    (unit_home / ".env").write_text(f"MEIVA_DB_URL={db_url}\n")
    root = unit_home / "cleanup"
    root.mkdir()
    complete = mode in ("complete", "blocked-completion")
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('cleanup', %s, 'python schedule.py', true, 'stopped') RETURNING id",
        (_scripts(root, complete=complete),),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    schedule_id = row[0]
    with (
        _processes(root) as tracked,
        (root / "runner.log").open("w") as log,
        _locked(db_url, "schedules", schedule_id)
        if mode.startswith("blocked-")
        else _no_lock() as lock,
    ):
        # Use a test-owned group so a buggy group kill fails without harming pytest.
        # The sibling models a PTY shell's other job in the runner's group.
        sibling = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
        )
        tracked.append(psutil.Process(sibling.pid))
        runner = subprocess.Popen(  # noqa: S603 - fixed test harness and fixture schedule id
            [sys.executable, str(_CHILD), "run", str(root), str(schedule_id), mode],
            stdout=log,
            stderr=log,
            process_group=sibling.pid,
            env={**os.environ, "MEIVA_SCHEDULE_STALL_TIMEOUT_SECONDS": "0.15"},
        )
        tracked.append(psutil.Process(runner.pid))
        owned = _capture_children(root, runner.pid)
        tracked.extend(owned)
        assert os.getpgid(runner.pid) == os.getpgid(sibling.pid)
        (root / "release").touch()
        if mode == "blocked-record":
            _await_blocked_write(db_conn, "schedules")
            # meiva:note
            # The failure write is still pending: the cleanup came first.
            _assert_reaped(root, owned)
        if mode == "blocked-completion":
            _await_file(root / "park-finished")
            # meiva:note
            # the completion write has now outlasted the stall budget
            time.sleep(0.6)
        if lock is not None:
            lock.release()
        assert runner.wait(timeout=15) == (0 if complete else 1), (root / "runner.log").read_text()
        assert (root / "park-finished").exists(), "guard killed a legitimate park"
        assert sibling.poll() is None, "cleanup signalled an unrelated group member"
        if complete:
            _assert_untouched(root, owned)
        else:
            _assert_reaped(root, owned)
    run_row = db_conn.execute(
        "SELECT ok, note FROM schedule_runs WHERE schedule_id = %s", (schedule_id,)
    ).fetchone()
    assert run_row == ((True, None) if complete else (False, "stalled (0s)"))


@contextmanager
def _no_lock() -> Generator[None]:
    yield None


# meiva:note
# A schedule row and an open run row for the child to fail against.
def _blocked_scenario(
    db_conn: psycopg.Connection, db_url: str, unit_home: Path, *, script: str = "pass"
) -> tuple[int, int]:
    (unit_home / ".env").write_text(f"MEIVA_DB_URL={db_url}\n")
    schedule = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status) "
        "VALUES ('bounded', %s, 'python schedule.py', true, 'stopped') RETURNING id",
        (script,),
    ).fetchone()
    assert schedule is not None
    run = db_conn.execute(
        "INSERT INTO schedule_runs (schedule_id) VALUES (%s) RETURNING id", (schedule[0],)
    ).fetchone()
    db_conn.commit()
    assert run is not None
    return schedule[0], run[0]


# meiva:note
# A blocked DB write cannot keep a stalled runner alive past the record budget.
#
# Blocking the `schedules` row stops the first write (the failure text); blocking the run row
# lets the first write through and stops the second.
@pytest.mark.parametrize(
    ("blocked_table", "recorded"), [("schedules", None), ("schedule_runs", "test stall")]
)
def test_stall_exit_bounds_failure_records(
    db_conn: psycopg.Connection,
    db_url: str,
    unit_home: Path,
    tmp_path: Path,
    blocked_table: Literal["schedules", "schedule_runs"],
    recorded: str | None,
) -> None:
    schedule_id, run_id = _blocked_scenario(db_conn, db_url, unit_home)
    blocked_id = schedule_id if blocked_table == "schedules" else run_id
    with _locked(db_url, blocked_table, blocked_id) as lock:
        with subprocess.Popen(  # noqa: S603 - fixed test script
            [
                sys.executable,
                str(_CHILD),
                "stall_exit",
                str(tmp_path),
                str(schedule_id),
                str(run_id),
            ]
        ) as runner:
            try:
                _await_file(tmp_path / "started")
                assert runner.wait(timeout=5) == 1
                elapsed = time.monotonic() - float((tmp_path / "started").read_text())
                assert 0.9 <= elapsed < 1.8, f"record deadline took {elapsed:.2f}s"
            finally:
                runner.kill()
        lock.release()
    last_error = db_conn.execute(
        "SELECT last_error FROM schedules WHERE id = %s", (schedule_id,)
    ).fetchone()
    assert last_error == (recorded,)
    assert db_conn.execute("SELECT ok FROM schedule_runs WHERE id = %s", (run_id,)).fetchone() == (
        None,
    )


# meiva:note
# The script blocks until released, so the guard really stalls; the script then returns while
# the stall action is blocked on its first write. The hard exit still wins: the process exits 1
# inside the record budget and the schedule is never marked completed.
def test_a_script_that_returns_during_the_stall_action_cannot_complete(
    db_conn: psycopg.Connection, db_url: str, unit_home: Path, tmp_path: Path
) -> None:
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    script = (
        "from pathlib import Path\n"
        f"open({str(gate)!r}).read()\n"
        f"Path({str(tmp_path / 'script-returned')!r}).touch()\n"
    )
    schedule_id, _run_id = _blocked_scenario(db_conn, db_url, unit_home, script=script)
    with _locked(db_url, "schedules", schedule_id) as lock:
        # meiva:note
        # fixed test script.
        with subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                str(_CHILD),
                "completion_during_stall",
                str(tmp_path),
                str(schedule_id),
            ]
        ) as runner:
            try:
                _await_blocked_write(db_conn, "schedules")
                # meiva:note
                # opening the write end releases the script's read
                with gate.open("w"):
                    pass
                assert runner.wait(timeout=10) == 1
            finally:
                runner.kill()
        lock.release()
    assert (tmp_path / "script-returned").exists()
    status = db_conn.execute(
        "SELECT status FROM schedules WHERE id = %s", (schedule_id,)
    ).fetchone()
    assert status is not None and status[0] != "completed"
