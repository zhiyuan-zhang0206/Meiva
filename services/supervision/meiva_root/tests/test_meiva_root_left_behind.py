# meiva:note
# A root reclaims the generations its predecessor left behind (task #5474).

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import psutil
import pytest

from services.supervision.meiva_root import unit_ledger
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.stopping import SupervisorConfig
from services.supervision.meiva_root.supervisor import Supervisor
from services.supervision.meiva_root.tests.slices import system_clock

_CHILD = Path(__file__).with_name("child_unit.py")


def _root(run_dir: Path, *scenario: str, stop_timeout_s: float = 0.2) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
    )
    return Supervisor(
        UnitRegistry([unit]),
        environ=dict(os.environ),
        run_dir=run_dir,
        config=SupervisorConfig(stop_timeout_s=stop_timeout_s),
        clock=system_clock(),
        root_pid=os.getpid(),
    )


# meiva:note
# A live process this root did not spawn, standing in for a dead root's child.
def _leftover(*scenario: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", str(_CHILD), *scenario]
    )


def _record(run_dir: Path, process: subprocess.Popen[bytes]) -> None:
    unit_ledger.write(run_dir, "worker", process.pid)


def _row(status: dict[str, object]) -> dict[str, object]:
    units = status["units"]
    assert isinstance(units, list)
    return cast("dict[str, object]", units[0])


async def _wait_ready(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"leftover did not signal readiness at {path}")


async def test_recorded_leftover_is_stopped_before_the_replacement_spawns(tmp_path: Path) -> None:
    leftover = _leftover("sleep")
    try:
        _record(tmp_path, leftover)
        owner = _root(tmp_path, "sleep")
        await owner.start()
        try:
            assert leftover.wait(timeout=5) is not None
            row = _row(await owner.status())
            assert row["state"] == "running"
            assert row["pid"] != leftover.pid
            record = unit_ledger.read(tmp_path, "worker")
            assert record is not None
            assert record == row["pid"]
        finally:
            await owner.shutdown()
    finally:
        if leftover.poll() is None:
            leftover.kill()
            leftover.wait(timeout=5)


async def test_recorded_leftover_that_ignores_term_is_killed(tmp_path: Path) -> None:
    ready = tmp_path / "leftover-ready"
    leftover = _leftover("ignore_term", str(ready))
    try:
        await _wait_ready(ready)
        _record(tmp_path, leftover)
        owner = _root(tmp_path, "sleep")
        await owner.start()
        try:
            assert _row(await owner.status())["state"] == "running"
            assert not psutil.pid_exists(leftover.pid) or leftover.poll() is not None
        finally:
            await owner.shutdown()
    finally:
        if leftover.poll() is None:
            leftover.kill()
            leftover.wait(timeout=5)


async def test_a_process_no_record_describes_is_never_signalled(tmp_path: Path) -> None:
    bystander = _leftover("sleep")
    try:
        owner = _root(tmp_path, "sleep")
        await owner.start()
        try:
            assert _row(await owner.status())["state"] == "running"
            assert bystander.poll() is None
        finally:
            await owner.shutdown()
    finally:
        if bystander.poll() is None:
            bystander.kill()
            bystander.wait(timeout=5)


async def test_a_record_whose_process_is_gone_is_dropped(tmp_path: Path) -> None:
    dead = _leftover("sleep")
    _record(tmp_path, dead)
    dead.kill()
    dead.wait(timeout=5)
    owner = _root(tmp_path, "sleep")
    await owner.start()
    try:
        row = _row(await owner.status())
        assert row["state"] == "running"
        record = unit_ledger.read(tmp_path, "worker")
        assert record is not None
        assert record == row["pid"]
    finally:
        await owner.shutdown()


async def test_the_record_is_cleared_when_its_generation_exits(tmp_path: Path) -> None:
    marker = tmp_path / "gate-exit"
    owner = _root(tmp_path, "code_gate_exit", str(marker))
    await owner.start()
    try:
        for _ in range(200):
            if unit_ledger.read(tmp_path, "worker") is None:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("an exited generation kept its spawn record")
    finally:
        await owner.shutdown()


def test_a_late_clear_from_an_older_generation_keeps_the_newer_record(tmp_path: Path) -> None:
    head = _leftover("sleep")
    try:
        unit_ledger.write(tmp_path, "worker", head.pid)
        unit_ledger.clear(tmp_path, "worker", pid=999999)
        kept = unit_ledger.read(tmp_path, "worker")
        assert kept is not None and kept == head.pid
        unit_ledger.clear(tmp_path, "worker", pid=head.pid)
        assert unit_ledger.read(tmp_path, "worker") is None
    finally:
        if head.poll() is None:
            head.kill()
            head.wait(timeout=5)


@pytest.mark.parametrize(
    "body",
    [
        "not a pid",
        "abc\n",
    ],
)
def test_an_unreadable_record_is_dropped_rather_than_trusted(tmp_path: Path, body: str) -> None:
    path = unit_ledger.pidfile_path(tmp_path, "worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    assert unit_ledger.read(tmp_path, "worker") is None
    assert not path.exists()
