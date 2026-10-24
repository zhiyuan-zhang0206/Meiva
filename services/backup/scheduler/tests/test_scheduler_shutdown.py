from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

import psutil
import pytest

from services.backup.layout import BackupTree
from services.backup.scheduler.settings import SchedulerSettings
from services.backup.scheduler.worker import WorkerLaunch, run_job

_DAEMON = Path(__file__).with_name("shutdown_daemon.py")
_WORKER = "services.backup.scheduler.tests.shutdown_worker"


@pytest.fixture
def postgres_base(tmp_path: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="bkpg-") as base:
        yield Path(base)
    pgdata = tmp_path / "pgdata"
    if pgdata.exists():
        assert not Path(pgdata.read_text()).exists()


def _wait_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while not path.exists() or not path.read_text().strip():
        assert process.poll() is None, process.communicate(timeout=2)
        assert time.monotonic() < deadline, f"never reached {path.name}"
        time.sleep(0.02)


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _terminate_and_assert_reaped(tmp_path: Path, process: subprocess.Popen[str]) -> None:
    _wait_file(tmp_path / "child", process)
    pids = [
        int(path.read_text()) for name in ("worker", "child") if (path := tmp_path / name).exists()
    ]
    assert (tmp_path / "run" / "pg_backup.pid").exists()
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=9)
    assert process.returncode == 0, stdout + stderr
    assert time.monotonic() - started < 9
    assert all(not _alive(pid) for pid in pids)


def _kill_harness_processes(tmp_path: Path, process: subprocess.Popen[str]) -> None:
    for name in ("child", "worker", "postgres"):
        path = tmp_path / name
        if path.exists() and (raw := path.read_text().strip()):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(raw), signal.SIGKILL)
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)


# meiva:link services/backup/scheduler/docs/timing-and-exit.meiva.okf.md
def _assert_sigterm_reaps_job_before_scheduler_exits(
    tmp_path: Path, mode: str, postgres_base: Path
) -> None:
    tree = BackupTree(tmp_path)
    tree.managed.mkdir(parents=True)
    retained = tree.managed / "retained.dump.enc"
    retained.write_bytes(b"previous complete backup")
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_DAEMON), str(tmp_path), mode, str(postgres_base)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=tmp_path,
    )
    try:
        _terminate_and_assert_reaped(tmp_path, process)
        assert not (tmp_path / "run" / "pg_backup.pid").exists()
        assert not tree.restore_marker.exists()
        assert retained.read_bytes() == b"previous complete backup"
        assert not list((tmp_path / "backups" / "operations").glob("*/.operation-*"))
        assert not (tmp_path / "backups" / "quarantine").exists()
        assert [path for path in tree.managed.iterdir() if path != retained] == (
            [] if not mode.startswith("restore") else list(tree.managed.glob("stubdb-*.dump.enc"))
        )
    finally:
        _kill_harness_processes(tmp_path, process)


@pytest.mark.parametrize("mode", ["block", "stubborn", "restore"])
def test_sigterm_reaps_job_before_scheduler_exits(
    tmp_path: Path, mode: str, postgres_base: Path
) -> None:
    _assert_sigterm_reaps_job_before_scheduler_exits(tmp_path, mode, postgres_base)


def test_killed_restore_uses_parent_owned_postgres_base(
    tmp_path: Path, postgres_base: Path
) -> None:
    _assert_sigterm_reaps_job_before_scheduler_exits(tmp_path, "restore-stubborn", postgres_base)

    assert Path((tmp_path / "pgdata").read_text()).is_relative_to(postgres_base)

    assert not list(postgres_base.glob("meiva-pg-*"))


async def test_restore_job_accepts_clean_foreground_postgres_exit(
    tmp_path: Path, postgres_base: Path
) -> None:
    settings = SchedulerSettings(
        timezone=ZoneInfo("UTC"), backup_hour=0, keep=7, throwaway_base=postgres_base
    )
    launch = WorkerLaunch(
        _WORKER,
        {
            **os.environ,
            "MEIVA_TEST_BACKUP_ROOT": str(tmp_path),
            "MEIVA_TEST_BACKUP_MODE": "roundtrip",
            "MEIVA_TEST_BACKUP_POSTGRES_BASE": str(postgres_base),
        },
    )
    tree = BackupTree(tmp_path)

    await run_job("restore", settings=settings, tree=tree, launch=launch)

    assert not _alive(int((tmp_path / "postgres").read_text()))
    controls = tree.controls("restore-drill")
    assert [path.name for path in controls.iterdir()] == [".lock"]
