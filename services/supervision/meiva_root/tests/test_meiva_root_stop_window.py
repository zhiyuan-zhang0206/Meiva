from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from time import monotonic
from typing import Any, cast

import psutil
import pytest

from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import RootClient, caller_identity
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.server import ControlServer
from services.supervision.meiva_root.stopping import UnitStopTimeoutError, signal_owned
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig
from services.supervision.meiva_root.tests.slices import system_clock

_DEFAULT_WINDOW_S = 0.2


_CHILD = Path(__file__).with_name("child_unit.py")


def root(
    run_dir: Path,
    *scenario: str,
    stop_timeout_s: float | None = None,
    default_window_s: float = _DEFAULT_WINDOW_S,
) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=stop_timeout_s,
    )
    return Supervisor(
        UnitRegistry([unit]),
        environ=dict(os.environ),
        run_dir=run_dir,
        config=SupervisorConfig(stop_timeout_s=default_window_s),
        clock=system_clock(),
        root_pid=os.getpid(),
    )


class _Recorder:
    def __init__(self, records: LogRecords) -> None:
        self._records = records

    def events(self, name: str) -> list[dict[str, Any]]:
        return [dict(r["extra"]) for r in self._records if r["extra"].get("event") == name]


@pytest.fixture
def recorder(loguru_records: LogRecords) -> _Recorder:
    return _Recorder(loguru_records)


async def state(owner: Supervisor) -> str:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]["state"]


async def wait_file(path: Path) -> None:
    for _ in range(250):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"child did not write {path}")


def closes_slowly_after_term(ready: Path, close_s: float) -> tuple[str, ...]:
    return ("closes_slowly", str(ready), str(close_s))


async def test_unit_closing_past_the_default_window_stops_inside_its_declared_window(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, *closes_slowly_after_term(ready, 0.8), stop_timeout_s=3.0)
    await owner.start()
    await wait_file(ready)
    result = await owner.down("worker")
    assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
    assert await state(owner) == "stopped"
    assert not (tmp_path / "custody").exists()
    await owner.shutdown()


async def test_unit_overrunning_the_default_window_is_killed_not_left_running(
    tmp_path: Path, recorder: _Recorder
) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, *closes_slowly_after_term(ready, 3.0))
    await owner.start()
    await wait_file(ready)
    try:
        result = await owner.down("worker")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
        assert await state(owner) == "stopped"
        assert len(recorder.events("root_unit_stop_escalated")) == 1
        assert recorder.events("root_unit_stop_aborted") == []
    finally:
        await owner.shutdown()


async def test_window_expiry_kills_once_and_reports_it(tmp_path: Path, recorder: _Recorder) -> None:
    ready = tmp_path / "ready"
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), "ignore_term", str(ready)),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=0.6,
    )
    owner = Supervisor(
        UnitRegistry([unit]),
        environ=dict(os.environ),
        run_dir=tmp_path,
        config=SupervisorConfig(),
        clock=system_clock(),
        root_pid=os.getpid(),
    )
    await owner.start()
    await wait_file(ready)
    pid = cast("list[dict[str, Any]]", (await owner.status())["units"])[0]["pid"]
    try:
        began = monotonic()
        await owner.down("worker")
        assert monotonic() - began >= 0.6, "killed before the hard budget elapsed"
        assert not psutil.pid_exists(pid)
        assert await state(owner) == "stopped"
        escalated = recorder.events("root_unit_stop_escalated")
        assert len(escalated) == 1
        assert escalated[0]["generation"] == 1
        assert escalated[0]["reason"] == "escaped_term_window"
        assert escalated[0]["pid"] == pid
        assert recorder.events("root_unit_stop_aborted") == []
    finally:
        await owner.shutdown()


def test_identity_change_never_signals_and_reports_the_abort() -> None:
    stale = OwnedProcess(pid=2**22 + 7, birth=0.0, starttime=None)
    assert stale.live() is False
    assert signal_owned(stale, force=True, own_group=os.getpgrp()) is False


async def test_an_uncaptured_leader_identity_reports_its_own_reason(
    tmp_path: Path, recorder: _Recorder
) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, "ignore_term", str(ready), stop_timeout_s=0.1)
    await owner.start()
    await wait_file(ready)
    generation = owner._units["worker"].generation
    assert generation is not None
    generation.identity = None
    try:
        with pytest.raises(UnitStopTimeoutError, match="never captured its leader identity"):
            await owner.down("worker")
        aborted = recorder.events("root_unit_stop_aborted")
        assert [event["reason"] for event in aborted] == ["identity_unavailable"]
        assert recorder.events("root_unit_stop_escalated") == []
    finally:
        generation.proc.kill()
        await generation.exited.wait()
        await owner.shutdown()


def test_reap_window_rejects_a_non_positive_value() -> None:
    with pytest.raises(ValueError, match="stop_kill_reap_s"):
        SupervisorConfig(stop_kill_reap_s=0.0)


async def test_force_kills_without_waiting_the_declared_window(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    owner = root(tmp_path, "ignore_term", str(ready), stop_timeout_s=0.8)
    await owner.start()
    await wait_file(ready)
    pid = cast("list[dict[str, Any]]", (await owner.status())["units"])[0]["pid"]
    began = monotonic()
    await owner.down("worker", force=True)
    assert monotonic() - began < 0.8, "force waited the unit's graceful window anyway"
    assert not psutil.pid_exists(pid)
    await owner.shutdown()


async def test_down_over_the_control_socket_succeeds_for_a_unit_closing_past_ten_seconds(
    short_tmp: Path,
) -> None:
    ready = short_tmp / "ready"
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *closes_slowly_after_term(ready, 12.5)),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=20.0,
    )
    owner = Supervisor(
        UnitRegistry([unit]),
        environ=dict(os.environ),
        run_dir=short_tmp,
        clock=system_clock(),
        root_pid=os.getpid(),
    )
    server = ControlServer(short_tmp / "meiva-root.sock", owner.dispatch)
    await server.start()
    try:
        await owner.start()
        await wait_file(ready)
        response = await asyncio.to_thread(
            RootClient(
                short_tmp / "meiva-root.sock",
                caller=caller_identity({}, pid=os.getpid()),
                timeout=60,
            ).down,
            "worker",
        )
        assert response["ok"] is True, response
        assert await state(owner) == "stopped"
        assert not (short_tmp / "custody").exists()
    finally:
        await owner.shutdown()
        await server.close()


@pytest.mark.parametrize("mode", ["down", "shutdown"])
async def test_stop_tiers_drain_strictly_in_order_across_down_and_shutdown(
    tmp_path: Path, mode: str
) -> None:
    events = tmp_path / "events.log"
    specs = [
        ("browser", 2, 0.05),
        ("worker-a", 1, 0.1),
        ("agent-host", 0, 0.15),
        ("worker-b", 1, 0.1),
    ]
    manifests = [
        UnitManifest(
            name,
            (
                sys.executable,
                "-u",
                str(_CHILD),
                "closes_recording",
                str(tmp_path / f"ready-{name}"),
                str(events),
                name,
                str(delay_s),
            ),
            RestartPolicy.ALWAYS,
            "root",
            stop_timeout_s=5.0,
            stop_tier=tier,
        )
        for name, tier, delay_s in specs
    ]
    owner = Supervisor(
        UnitRegistry(manifests),
        environ=dict(os.environ),
        run_dir=tmp_path / "run",
        clock=system_clock(),
        root_pid=os.getpid(),
    )
    await owner.start()
    for name, _, _ in specs:
        await wait_file(tmp_path / f"ready-{name}")
    try:
        if mode == "down":
            result = await owner.down("root")
            units = cast("list[dict[str, object]]", result["units"])
            assert [row["id"] for row in units] == ["agent-host", "worker-a", "worker-b", "browser"]
            assert all(row["action"] == "stopped" for row in units)
        else:
            await owner.shutdown()
        recorded = events.read_text(encoding="utf-8").splitlines()
        assert recorded[:2] == ["term agent-host", "exit agent-host"]
        assert set(recorded[2:6]) == {
            "term worker-a",
            "exit worker-a",
            "term worker-b",
            "exit worker-b",
        }
        assert recorded[6:] == ["term browser", "exit browser"]
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("mode", ["down_root", "concurrent_down", "shutdown"])
async def test_same_tier_slow_units_stop_in_parallel_preserving_result_order(
    tmp_path: Path, mode: str
) -> None:
    events = tmp_path / "events.log"
    names = ("alpha", "beta", "gamma")
    manifests = [
        UnitManifest(
            name,
            (
                sys.executable,
                "-u",
                str(_CHILD),
                "closes_recording",
                str(tmp_path / f"ready-{name}"),
                str(events),
                name,
                "0.3",
            ),
            RestartPolicy.ALWAYS,
            "root",
            stop_timeout_s=5.0,
            stop_tier=1,
        )
        for name in names
    ]
    owner = Supervisor(
        UnitRegistry(manifests),
        environ=dict(os.environ),
        run_dir=tmp_path / "run",
        clock=system_clock(),
        root_pid=os.getpid(),
    )
    await owner.start()
    for name in names:
        await wait_file(tmp_path / f"ready-{name}")
    try:
        if mode == "down_root":
            result = await asyncio.wait_for(owner.down("root"), timeout=0.55)
            units = cast("list[dict[str, object]]", result["units"])
            assert [row["id"] for row in units] == list(names)
            assert all(row["action"] == "stopped" for row in units)
        elif mode == "concurrent_down":
            results = await asyncio.wait_for(
                asyncio.gather(*(owner.down(name) for name in names)), timeout=0.55
            )
            assert [cast("list[dict[str, object]]", r["units"])[0]["id"] for r in results] == list(
                names
            )
        else:
            await asyncio.wait_for(owner.shutdown(), timeout=0.55)
        recorded = events.read_text(encoding="utf-8").splitlines()
        assert set(recorded[:3]) == {f"term {name}" for name in names}
        assert set(recorded[3:]) == {f"exit {name}" for name in names}
    finally:
        await owner.shutdown()
