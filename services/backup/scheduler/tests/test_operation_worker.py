# meiva:note
# Backup worker happy path, cancellation, private cleanup and result validation.
#
# Every worker here is a real child process (`stub_worker.py`) launched by the production
# controller through its real bootstrap, so launch, wait, stop and cleanup are not stand-ins.

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.db import Database
from services.backup.layout import BackupTree
from services.backup.scheduler.operation import worker_process as workers
from services.backup.scheduler.operation.staging import cleanup
from services.backup.scheduler.settings import DumpSettings
from services.backup.scheduler.tests.operation_support import (
    STUB_WORKER,
    operation_kind,
    until,
)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"kind": "restore", "now": None}, ValueError),
        ({"kind": "restore", "now": None, "pg_throwaway_base": 42}, TypeError),
        ({"kind": "restore", "now": None, "pg_throwaway_base": []}, TypeError),
    ],
)
def test_restore_worker_requires_the_captured_throwaway_base(
    tmp_path: Path, payload: dict[str, object], error: type[Exception]
) -> None:
    from services.backup.scheduler import worker

    def database() -> Database:
        raise AssertionError("an invalid request must fail before database access")

    def database_for_url(_url: str) -> Database:
        return database()

    with pytest.raises(error):
        worker.execute(
            payload,
            tmp_path,
            settings=DumpSettings(
                is_remote=False,
                plane_url="postgresql://unused.invalid/db",
                keep=7,
                offsite_endpoint="",
                offsite_bucket="",
                offsite_credentials_file=None,
            ),
            tree=BackupTree(tmp_path),
            database=database,
            database_for_url=database_for_url,
        )
    assert not (tmp_path / "scratch").exists()


async def test_zero_exit_valid_result_is_committed_and_staging_removed(tmp_path: Path) -> None:
    completed = await workers.run_operation(
        STUB_WORKER, {"mode": "ok"}, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
    )

    assert completed.result == {"ok": True}
    assert completed.work.stat().st_mode & 0o777 == 0o700
    assert (completed.work / "request.json").stat().st_mode & 0o777 == 0o600
    assert await completed.commit(lambda: "published") == "published"
    assert not completed.work.exists()


@pytest.mark.parametrize("payload,code", [("{}", 3), ("invalid", 0), ("[]", 0), (None, 0)])
async def test_worker_failure_cleans_staging_without_blocking_next_run(
    tmp_path: Path, payload: str | None, code: int
) -> None:
    request = {"mode": "exit", "code": code, "payload": payload}
    for _ in range(2):
        with pytest.raises((RuntimeError, TypeError)):
            await workers.run_operation(
                STUB_WORKER, request, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
            )
        assert not list((tmp_path / "controls").glob(".operation-*"))
    assert not list(tmp_path.rglob("unresolved.json"))


async def test_cancel_unwinds_worker_plaintext_cleanup_and_releases_kind_lock(
    tmp_path: Path,
) -> None:
    ready, plaintext = tmp_path / "ready", tmp_path / "plaintext"
    request = {"mode": "plaintext-then-hang", "plaintext": str(plaintext), "ready": str(ready)}
    task = asyncio.create_task(
        workers.run_operation(
            STUB_WORKER, request, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
        )
    )
    await until(ready)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not plaintext.exists()
    assert not list((tmp_path / "controls").glob(".operation-*"))
    completed = await workers.run_operation(
        STUB_WORKER, {"mode": "ok"}, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
    )
    await completed.commit(lambda: None)


async def test_timeout_bounds_worker_and_removes_its_plaintext_scratch(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    request = {"mode": "scratch-then-hang", "ready": str(ready)}
    kind = operation_kind(tmp_path, plaintext_scratch=True)

    with pytest.raises(TimeoutError, match="execution bound"):
        await workers.run_operation(
            STUB_WORKER, request, kind=kind, env={}, timeout_s=3.0, timers=SYSTEM_TIMERS
        )

    assert ready.exists(), "the worker staged its plaintext before the bound expired"
    assert not list((tmp_path / "controls").glob(".operation-*"))


async def test_a_staging_directory_that_cannot_be_removed_reports_its_private_path(
    tmp_path: Path,
) -> None:
    completed = await workers.run_operation(
        STUB_WORKER, {"mode": "ok"}, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
    )
    shutil.rmtree(completed.work)

    with pytest.raises(FileNotFoundError) as caught:
        await completed.commit(lambda: None)

    assert str(completed.work) in "\n".join(caught.value.__notes__)


async def test_old_receipts_do_not_block_independent_scratch(tmp_path: Path) -> None:
    old = tmp_path / "controls/.operation-old"
    old.mkdir(parents=True)
    (old / "unresolved.json").write_text("{}")

    completed = await workers.run_operation(
        STUB_WORKER, {"mode": "ok"}, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
    )

    assert completed.work != old
    await completed.commit(lambda: None)
    assert old.exists(), "old private scratch is not adopted or silently deleted"


async def test_commit_runs_its_step_off_the_event_loop(tmp_path: Path) -> None:
    completed = await workers.run_operation(
        STUB_WORKER, {"mode": "ok"}, kind=operation_kind(tmp_path), env={}, timers=SYSTEM_TIMERS
    )
    ticks = 0
    during: list[int] = []

    def slow_step() -> str:
        started = ticks
        time.sleep(0.2)
        during.append(ticks - started)
        return "published"

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        assert await completed.commit(slow_step) == "published"
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker
    assert during and during[0] >= 5


async def test_a_failed_operation_whose_staging_vanished_keeps_its_path_in_the_report(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(FileNotFoundError) as caught:
        await workers.run_operation(
            STUB_WORKER,
            {"mode": "exit-and-remove-work"},
            kind=operation_kind(tmp_path),
            env={},
            timers=SYSTEM_TIMERS,
        )

    assert "backup staging cleanup failed at" in caplog.text
    [work] = [line for line in caught.value.__notes__ if "operation cleanup failed at" in line]
    assert str(tmp_path / "controls" / ".operation-") in work


async def test_busy_concurrency_lock_defers_without_creating_staging(tmp_path: Path) -> None:
    from base.native_process.os_platform import file_lock
    from services.backup.scheduler.operation.staging import OperationBusyError

    kind = operation_kind(tmp_path)
    kind.control_root.mkdir()
    with file_lock(kind.control_root / ".lock"), pytest.raises(OperationBusyError):
        await workers.run_operation(
            STUB_WORKER, {"mode": "ok"}, kind=kind, env={}, timers=SYSTEM_TIMERS
        )
    assert not list(kind.control_root.glob(".operation-*"))


def test_cleanup_of_a_vanished_directory_raises_with_the_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as caught:
        cleanup(operation_kind(tmp_path), tmp_path / "gone")

    assert str(tmp_path / "gone") in "\n".join(caught.value.__notes__)
