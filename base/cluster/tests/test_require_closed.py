import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest

from base.cluster import postgres as pg


@dataclass(frozen=True)
class _Uids:
    real: int


# meiva:note
# One process as `require_closed` looks at it: every read answers from the script, and a read can
# be refused the way an exiting process refuses it.
class ScriptedProcess:
    def __init__(
        self,
        *,
        pid: int = 321,
        name: str = "postgres",
        argv: list[str] | None = None,
        cwd: Path | None,
        refuses_uids: bool = False,
        refuses_argv: bool = False,
        running: bool = True,
        status: str = psutil.STATUS_RUNNING,
    ) -> None:
        self.pid = pid
        self._name = name
        self._argv = argv if argv is not None else ["postgres: checkpointer"]
        self._cwd = cwd
        self._refuses_uids = refuses_uids
        self._refuses_argv = refuses_argv
        self._running = running
        self._status = status

    def uids(self) -> _Uids:
        if self._refuses_uids:
            raise psutil.AccessDenied(self.pid, self._name)
        return _Uids(real=os.getuid())

    def status(self) -> str:
        return self._status

    def name(self) -> str:
        return self._name

    def cwd(self) -> str:
        return "" if self._cwd is None else str(self._cwd)

    def cmdline(self) -> list[str]:
        if self._refuses_argv:
            raise psutil.AccessDenied(self.pid, "postgres")
        return self._argv

    def is_running(self) -> bool:
        return self._running


def test_a_worker_inside_the_data_directory_retains_custody(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(RuntimeError, match="retain custody"):
        pg.require_closed(data, None, processes=[ScriptedProcess(cwd=data)])


def test_a_process_that_exits_while_refusing_its_argv_read_is_not_custody(tmp_path: Path) -> None:
    data = tmp_path / "data"
    elsewhere = tmp_path / "elsewhere"
    data.mkdir()
    elsewhere.mkdir()
    # meiva:note
    # The first status read precedes exit; the later liveness read observes that the process
    # vanished after the argv refusal. A zombie from the outset would skip that branch entirely.
    exiting = ScriptedProcess(cwd=elsewhere, refuses_argv=True, running=False)

    pg.require_closed(data, None, processes=[exiting])


def test_a_running_process_whose_argv_cannot_be_read_cannot_be_proven_closed(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    elsewhere = tmp_path / "elsewhere"
    data.mkdir()
    elsewhere.mkdir()
    unreadable = ScriptedProcess(cwd=elsewhere, refuses_argv=True, running=True)

    with pytest.raises(psutil.AccessDenied):
        pg.require_closed(data, None, processes=[unreadable])


# meiva:note
# A receipt for this data directory that names no live birth: `require_closed` then judges every
# process by its own cwd and argv, which is the path these cases exercise. A receipt with no owner
# never reaches the boot-scope comparison, so the platform it records is not read here — naming one
# keeps the host out of a fixture whose subject is a process, not a system.
def _receipt(data: Path) -> pg.Receipt:
    info = data.stat()
    return pg.Receipt(
        data=str(data),
        directory=(info.st_dev, info.st_ino),
        port=15433,
        platform="linux",
        boot_id="boot",
        state="not-started",
    )


def test_a_process_that_reports_no_working_directory_is_not_custody(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    pg.require_closed(data, _receipt(data), processes=[ScriptedProcess(cwd=None)])


_READY_TIMEOUT_S = 10.0


# meiva:note
# A process the kernel still gives a working directory for, started under the name `postgres`: the
# custody check reads that name before it compares directories, so only this shape reaches it.
# `sleep` holds still without writing anything, and the symlink is what makes the kernel's own name
# for the process `postgres` rather than `sleep`.
def _wait_for_postgres_name(postgres: Path, worker: psutil.Process) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while worker.name() != "postgres":
        if time.monotonic() >= deadline:
            raise AssertionError(f"{postgres} never ran under that name within {_READY_TIMEOUT_S}s")
        time.sleep(0.02)


def test_a_process_whose_working_directory_no_longer_exists_is_not_custody(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    postgres = tmp_path / "postgres"
    postgres.symlink_to("/bin/sleep")
    # meiva:note
    # This interpreter is the child's own executable through the symlink, so the run needs no
    # shell: the linter's subprocess rule is about building a command line, and this one is fixed.
    child = subprocess.Popen(  # noqa: S603
        [str(postgres), "60"],
        cwd=gone,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        worker = psutil.Process(child.pid)
        _wait_for_postgres_name(postgres, worker)
        gone.rmdir()
        assert worker.cwd() == str(gone)
        assert not gone.exists()

        pg.require_closed(data, _receipt(data), processes=[worker])
    finally:
        child.kill()
        child.wait(timeout=10)
