import os
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
        cwd: Path,
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
        return str(self._cwd)

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
