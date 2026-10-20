# meiva:note
# A root reclaims the generations its predecessor left behind (task #5474).

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import psutil
import pytest

from base.daemon.health import DaemonProbe
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root import unit_ledger
from services.supervision.meiva_root.failure_state import UnitFailureFacts
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.probes import ProbeRegistry
from services.supervision.meiva_root.stopping import SupervisorConfig
from services.supervision.meiva_root.supervisor import Supervisor

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
        run_dir=run_dir,
        config=SupervisorConfig(stop_timeout_s=stop_timeout_s),
    )


# meiva:note
# A live process this root did not spawn, standing in for a dead root's child.
def _leftover(*scenario: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-u", str(_CHILD), *scenario]
    )


def _record(run_dir: Path, process: subprocess.Popen[bytes], *, generation: int = 7) -> None:
    unit_ledger.write(
        run_dir,
        "worker",
        unit_ledger.GenerationRecord(
            identity=OwnedProcess.capture(psutil.Process(process.pid)),
            generation=generation,
            manifest_digest="recorded",
            started_at=time.time(),
        ),
    )


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
            assert record.identity.pid == row["pid"]
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
        assert record.identity.pid == row["pid"]
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
        _record(tmp_path, head, generation=8)
        unit_ledger.clear(tmp_path, "worker", generation=7)
        kept = unit_ledger.read(tmp_path, "worker")
        assert kept is not None and kept.generation == 8
        unit_ledger.clear(tmp_path, "worker", generation=8)
        assert unit_ledger.read(tmp_path, "worker") is None
    finally:
        if head.poll() is None:
            head.kill()
            head.wait(timeout=5)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"pid": 1}',
        '{"pid": 1, "birth": 1.0, "starttime": null, "generation": 1, '
        '"manifest_digest": "x", "started_at": 1.0, "extra": true}',
    ],
)
def test_an_unreadable_record_is_dropped_rather_than_trusted(tmp_path: Path, body: str) -> None:
    path = unit_ledger.record_path(tmp_path, "worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    assert unit_ledger.read(tmp_path, "worker") is None
    assert not path.exists()


class _Probe:
    def __init__(self, verdict: DaemonProbe) -> None:
        self.verdict = verdict

    def __call__(self) -> DaemonProbe:
        return self.verdict


# meiva:note
# The health runner's supervisor slice, owning no generation at all.
class _OwnerlessSupervisor:
    def __init__(self) -> None:
        self.generation: tuple[OwnedProcess, float] | None = None
        self.restart_calls: list[str] = []

    async def restart(self, unit_id: str) -> dict[str, object]:
        self.restart_calls.append(unit_id)
        return {"verb": "restart", "units": []}

    def health_generation(self, unit_id: str) -> tuple[OwnedProcess, float] | None:
        return self.generation

    def revival_deferral(self, unit_id: str) -> str | None:
        return None

    def unit_failure_facts(self, unit_id: str) -> UnitFailureFacts:
        return UnitFailureFacts(intent_running=True, restart_failed=None)


def _events(records: LogRecords, name: str) -> list[dict[str, object]]:
    return [r["extra"] for r in records if r["extra"].get("event") == name]


# meiva:note
# An alive verdict names the unit's daemon, never its owner: a leftover of a dead
# root answers identically. Reporting it once per episode is the only signal a
# process no spawn record describes can produce (task #5474).
async def test_an_alive_answer_from_no_generation_is_reported_once(
    loguru_records: LogRecords,
) -> None:
    probe = _Probe(DaemonProbe.up("leftover answers"))
    registry = ProbeRegistry()
    registry.register("svc", probe)
    stub = _OwnerlessSupervisor()
    monitor = HealthMonitor(
        stub,
        registry,
        config=HealthConfig(interval_s=0.01, startup_grace_s=0.0, restart_backoff_s=(0.0,)),
    )
    await monitor.run_round()
    await monitor.run_round()
    assert len(_events(loguru_records, "root_unit_unowned_alive")) == 1
    assert monitor.snapshot()["svc"].unowned_alive is True
    assert stub.restart_calls == []
    stub.generation = (OwnedProcess(43, 200.0, None), 0.0)
    await monitor.run_round()
    assert len(_events(loguru_records, "root_unit_owned_alive")) == 1
    assert monitor.snapshot()["svc"].unowned_alive is False
