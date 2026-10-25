from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from base.native_process.ownership import OwnedProcess
from services.supervision.meiva_root.inputs import InputSeal
from services.supervision.meiva_root.manifest import RestartPolicy, UnitManifest, UnitRegistry
from services.supervision.meiva_root.stopping import rotate_unit_log
from services.supervision.meiva_root.supervisor import Supervisor, SupervisorConfig

_CHILD = Path(__file__).with_name("child_unit.py")


def root(tmp_path: Path, *scenario: str, env: tuple[tuple[str, str], ...] = ()) -> Supervisor:
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), *scenario),
        RestartPolicy.ALWAYS,
        "root",
        env,
    )
    return Supervisor(
        UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig(stop_timeout_s=0.2)
    )


async def row(owner: Supervisor) -> dict[str, Any]:
    return cast("list[dict[str, Any]]", (await owner.status())["units"])[0]


async def wait_file(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"child did not write {path}")


async def test_parent_birth_and_private_environment_are_retained(tmp_path: Path) -> None:
    output = tmp_path / "environment"
    owner = root(
        tmp_path,
        "write_environment",
        str(output),
        "ROOT_TEST",
        env=(("ROOT_TEST", "projected"),),
    )
    await owner.start()
    try:
        await wait_file(output)
        unit = await row(owner)
        assert psutil.Process(unit["pid"]).ppid() == os.getpid()
        assert isinstance(unit["create_time"], float)
        assert output.read_text() == "projected"
        assert "projected" not in str(await owner.status())
        assert not (tmp_path / "custody").exists()
    finally:
        await owner.shutdown()


async def test_up_is_idempotent_and_restart_stops_previous_generation(tmp_path: Path) -> None:
    owner = root(tmp_path, "sleep")
    await owner.start()
    try:
        original = (await row(owner))["pid"]
        await owner.up("worker")
        assert (await row(owner))["pid"] == original
        await owner.restart("worker")
        assert (await row(owner))["pid"] != original
        assert not psutil.pid_exists(original)
    finally:
        await owner.shutdown()


@pytest.mark.parametrize("change", ["bytes", "added-file", "removed-file"])
async def test_restart_refuses_changed_inputs_before_native_birth(
    tmp_path: Path, change: str
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    value = config / "value"
    value.write_text("admitted")
    output = tmp_path / "read-config"
    unit = UnitManifest(
        "worker",
        (
            sys.executable,
            str(_CHILD),
            "read_config",
            str(output),
            str(value),
        ),
        RestartPolicy.ALWAYS,
        "root",
        inputs=(InputSeal.capture(config),),
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path / "root")
    await owner.start()
    try:
        await wait_file(output)
        assert output.read_text() == "admitted"
        original = (await row(owner))["pid"]
        if change == "bytes":
            value.write_text("unapproved")
        elif change == "added-file":
            (config / "new-rule").write_text("unapproved")
        else:
            value.unlink()
        output.unlink()
        await owner.restart("worker")
        current = await row(owner)
        assert current["state"] == "stopped"
        assert "service input changed" in current["last_error"]
        assert not psutil.pid_exists(original)
        assert not output.exists()
        assert not list((tmp_path / "root/custody").glob("*.json"))
        value.write_text("admitted")
        (config / "new-rule").unlink(missing_ok=True)
        await owner.up("worker")
        await wait_file(output)
        assert output.read_text() == "admitted"
    finally:
        await owner.shutdown()


async def test_a_child_ignoring_term_is_escalated_at_the_hard_budget(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    unit = UnitManifest(
        "worker",
        (sys.executable, "-u", str(_CHILD), "ignore_term", str(ready)),
        RestartPolicy.ALWAYS,
        "root",
        stop_timeout_s=0.5,
    )
    owner = Supervisor(UnitRegistry([unit]), run_dir=tmp_path, config=SupervisorConfig())
    await owner.start()
    await wait_file(ready)
    pid = (await row(owner))["pid"]
    try:
        result = await owner.down("worker")
        assert cast("list[dict[str, Any]]", result["units"])[0]["action"] == "stopped"
        assert not psutil.pid_exists(pid)
        assert not (tmp_path / "custody").exists()
    finally:
        await owner.shutdown()


def child_exited(child: psutil.Process) -> bool:
    try:
        return child.status() == psutil.STATUS_ZOMBIE or not child.is_running()
    except psutil.NoSuchProcess:
        return True


def test_child_exit_observation_accepts_a_reaped_process() -> None:
    # meiva:note
    # Spawn a fixed child helper process to observe reaped-process status.
    with subprocess.Popen(  # noqa: S603
        [sys.executable, str(_CHILD), "read_stdin_byte"],
        stdin=subprocess.PIPE,
    ) as process:
        child = psutil.Process(process.pid)
        process.communicate(input=b"exit", timeout=5)
    with pytest.raises(psutil.NoSuchProcess):
        child.status()
    assert child_exited(child)


async def test_normal_stop_signals_the_known_live_group(tmp_path: Path) -> None:
    child_file = tmp_path / "child"
    owner = root(tmp_path, "spawn_child", str(child_file))
    await owner.start()
    await wait_file(child_file)
    child = psutil.Process(int(child_file.read_text()))
    try:
        assert os.getpgid(child.pid) == (await row(owner))["pid"]
        assert os.getsid(child.pid) == os.getsid(0)
        await owner.down("worker")
        async with asyncio.timeout(2):
            while not child_exited(child):
                await asyncio.sleep(0.02)
        assert child_exited(child)
    finally:
        with contextlib.suppress(psutil.NoSuchProcess):
            if not child_exited(child):
                child.kill()
        await owner.shutdown()


async def test_unexpected_exit_can_be_replaced_without_custody_reconciliation(
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "exit"
    owner = root(
        tmp_path,
        "wait_for_file",
        str(trigger),
    )
    await owner.start()
    old = (await row(owner))["pid"]
    trigger.touch()
    generation = owner._units["worker"].generation
    assert generation is not None
    await asyncio.wait_for(generation.exited.wait(), 5)
    assert owner._units["worker"].generation is None
    assert owner.revival_deferral("worker") is None
    trigger.unlink()
    try:
        await owner.up("worker")
        assert (await row(owner))["pid"] != old
        assert (await row(owner))["state"] == "running"
    finally:
        await owner.shutdown()


def test_changed_birth_never_signals_a_numeric_group() -> None:
    from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
    from base.native_process.tests.fake_process_control import FakeProcessControl
    from services.supervision.meiva_root.stopping import signal_owned

    # meiva:note
    # Spawn a fixed sleeper helper process in its own group.
    stranger = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_CHILD), "sleep"], process_group=0
    )
    try:
        real = OwnedProcess.capture(psutil.Process(stranger.pid))
        stale = OwnedProcess(
            real.pid, real.birth - 1000, None if real.starttime is None else real.starttime - 1
        )
        pc = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)

        signal_owned(stale, force=False, process_control=pc)
        signal_owned(stale, force=True, process_control=pc)
        assert pc.signals == []
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait(timeout=5)


async def test_obsolete_custody_records_do_not_gate_ordinary_start(tmp_path: Path) -> None:
    directory = tmp_path / "custody"
    directory.mkdir()
    (directory / "worker.json").write_text("obsolete spawning record")
    owner = root(tmp_path, "sleep")
    await owner.start()
    try:
        assert (await row(owner))["state"] == "running"
        assert (await row(owner))["pid"] is not None
    finally:
        await owner.shutdown()


def test_concrete_group_signal_error_is_not_hidden() -> None:
    from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
    from base.native_process.tests.fake_process_control import FakeProcessControl
    from services.supervision.meiva_root.stopping import signal_owned

    # meiva:note
    # Spawn a fixed sleeper helper process in its own group.
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_CHILD), "sleep"], process_group=0
    )
    try:
        identity = OwnedProcess.capture(psutil.Process(child.pid))
        pc = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
        pc.refuse(child.pid, PermissionError("known group signal denied"))

        with pytest.raises(PermissionError, match="known group signal denied"):
            signal_owned(identity, force=False, process_control=pc)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


# meiva:note
# The assembly decides, the supervisor applies. These tests own both halves: an
# unconfigured unit launches exactly as written, and a configured transform is applied
# to that unit only -- no accidental wrapping of every unit.
async def test_an_unconfigured_unit_is_launched_without_a_rewrite(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def launcher(*argv: str, **options: Any) -> Any:
        seen.append(tuple(argv))
        raise OSError("launcher probe: nothing is started")

    owner = _probe_supervisor(tmp_path, launcher)
    await owner.start()
    try:
        assert seen == [(sys.executable, "-u", str(_CHILD), "sleep")]
        assert (await row(owner))["launch_rewrite"] == "off"
    finally:
        await owner.shutdown()


async def test_a_configured_transform_rewrites_only_its_unit(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def launcher(*argv: str, **options: Any) -> Any:
        seen.append(tuple(argv))
        raise OSError("launcher probe: nothing is started")

    def wrap(unit_id: str, argv: Sequence[str]) -> Sequence[str]:
        return [f"wrapped:{unit_id}", *argv] if unit_id == "worker" else list(argv)

    owner = _probe_supervisor(tmp_path, launcher)
    owner.set_argv_transform(wrap)
    await owner.start()
    try:
        # meiva:note
        # The launcher refuses everything here, so the rewrite is followed by the
        # unchanged fallback: the transform reached only the unit it named, twice.
        assert seen == [
            ("wrapped:worker", sys.executable, "-u", str(_CHILD), "sleep"),
            (sys.executable, "-u", str(_CHILD), "sleep"),
        ]
        assert (await row(owner))["launch_rewrite"] == "on"
    finally:
        await owner.shutdown()


def _probe_supervisor(tmp_path: Path, launcher: Any) -> Supervisor:
    unit = UnitManifest(
        "worker", (sys.executable, "-u", str(_CHILD), "sleep"), RestartPolicy.ALWAYS, "root", ()
    )
    return Supervisor(
        UnitRegistry([unit]),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.2),
        spawn=launcher,
    )


# meiva:note
# The rewrite is a preference, not a requirement: a launcher that refuses the wrapped
# argv must cost one failed attempt, not a unit that never starts.
async def test_a_refused_rewrite_falls_back_to_the_manifest_argv(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []

    async def launcher(*argv: str, **options: Any) -> Any:
        seen.append(tuple(argv))
        raise OSError("this machine cannot run that launcher")

    owner = _probe_supervisor(tmp_path, launcher)
    owner.set_argv_transform(lambda unit_id, argv: [f"wrapped:{unit_id}", *argv])
    await owner.start()
    try:
        assert seen == [
            ("wrapped:worker", sys.executable, "-u", str(_CHILD), "sleep"),
            (sys.executable, "-u", str(_CHILD), "sleep"),
        ]
    finally:
        await owner.shutdown()


# meiva:note
# The mirror of the retirement rule: a *stopped* unit also exits inside the window, and a
# signal death reports a negative code. Reading any exit as "the wrapper is unusable" would
# let one ordinary `stop` retire the cap for the rest of this root's life.
async def test_a_stopped_unit_keeps_its_rewrite(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []
    plain = (sys.executable, "-u", str(_CHILD), "sleep")

    async def launcher(*argv: str, **options: Any) -> Any:
        seen.append(tuple(argv))
        # meiva:note
        # Run the manifest argv whatever was asked for: the wrapper name is a probe, and the
        # point is a real child that dies of the stop signal.
        return await asyncio.create_subprocess_exec(*plain, **options)

    unit = UnitManifest("worker", plain, RestartPolicy.ALWAYS, "root", ())
    owner = Supervisor(
        UnitRegistry([unit]),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.2),
        spawn=launcher,
    )
    owner.set_argv_transform(lambda unit_id, argv: [f"wrapped:{unit_id}", *argv])
    await owner.start()
    try:
        await owner.down("worker")
        assert (await row(owner))["launch_rewrite"] == "on"
        await owner.up("worker")
        assert len(seen) == 2
        assert seen[1][0] == "wrapped:worker"
    finally:
        await owner.shutdown()


# meiva:note
# The dangerous shape is not "the launcher refuses" but "the launcher starts, then exits
# non-zero": that reads as an ordinary unit exit, so the restart policy would replay the
# same wrapper forever and the unit would never come up. One early exit must retire the
# rewrite and the next generation must launch the manifest argv.
async def test_an_early_non_zero_exit_retires_the_rewrite(tmp_path: Path) -> None:
    seen: list[tuple[str, ...]] = []
    plain = (sys.executable, "-u", str(_CHILD), "code_gate_exit", str(tmp_path / "gate"))

    async def launcher(*argv: str, **options: Any) -> Any:
        seen.append(tuple(argv))
        # meiva:note
        # Record what was asked for, but always run the manifest argv: the point is a
        # process that starts and then exits non-zero straight away.
        return await asyncio.create_subprocess_exec(*plain, **options)

    unit = UnitManifest("worker", plain, RestartPolicy.ALWAYS, "root", ())
    owner = Supervisor(
        UnitRegistry([unit]),
        run_dir=tmp_path,
        config=SupervisorConfig(stop_timeout_s=0.2),
        spawn=launcher,
    )
    owner.set_argv_transform(lambda unit_id, argv: [f"wrapped:{unit_id}", *argv])
    await owner.start()
    try:
        for _ in range(200):
            if (await row(owner))["state"] == "stopped":
                break
            await asyncio.sleep(0.02)
        assert (await row(owner))["launch_rewrite"] == "off(retired)"
        await owner.restart("worker")
        for _ in range(200):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.02)
        assert seen[0][0] == "wrapped:worker"
        assert seen[1] == plain
    finally:
        await owner.shutdown()


def _log_with_bytes(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size)
    return path


def test_unit_log_a_file_below_the_cap_is_left_alone(tmp_path: Path) -> None:
    log = _log_with_bytes(tmp_path / "unit" / "output.log", 10)

    assert rotate_unit_log(log, max_bytes=11, backups=3) is False
    assert log.read_text() == "x" * 10
    assert not (tmp_path / "unit" / "output.log.1").exists()


def test_unit_log_a_missing_file_is_not_created(tmp_path: Path) -> None:
    log = tmp_path / "unit" / "output.log"

    assert rotate_unit_log(log, max_bytes=1, backups=3) is False
    assert not log.exists()


def test_unit_log_reaching_the_cap_moves_the_file_aside_and_starts_a_fresh_one(
    tmp_path: Path,
) -> None:
    log = _log_with_bytes(tmp_path / "unit" / "output.log", 8)

    assert rotate_unit_log(log, max_bytes=8, backups=3) is True
    assert not log.exists()
    assert (tmp_path / "unit" / "output.log.1").read_text() == "x" * 8


def test_unit_log_rotation_shifts_generations_and_drops_the_oldest(tmp_path: Path) -> None:
    unit = tmp_path / "unit"
    log = unit / "output.log"
    for index, content in enumerate(("first", "second", "third"), start=1):
        _log_with_bytes(log, len(content))
        log.write_text(content)
        assert rotate_unit_log(log, max_bytes=1, backups=2) is True
        assert (unit / "output.log.1").read_text() == content
        if index >= 2:
            assert (unit / "output.log.2").exists()

    assert sorted(p.name for p in unit.iterdir()) == ["output.log.1", "output.log.2"]
    assert (unit / "output.log.1").read_text() == "third"
    assert (unit / "output.log.2").read_text() == "second"


def test_unit_log_a_rotation_failure_is_reported_not_raised(tmp_path: Path) -> None:
    unit = tmp_path / "unit"
    log = _log_with_bytes(unit / "output.log", 8)
    unit.chmod(0o500)
    try:
        assert rotate_unit_log(log, max_bytes=1, backups=3) is False
        assert log.exists()
    finally:
        unit.chmod(0o700)


def test_unit_log_cap_and_backups_are_supervisor_configuration() -> None:
    from services.supervision.meiva_root.stopping import SupervisorConfig

    config = SupervisorConfig()
    assert config.unit_log_max_bytes > 0
    assert config.unit_log_backups >= 1
    with pytest.raises(ValueError):
        SupervisorConfig(unit_log_max_bytes=0)
    with pytest.raises(ValueError):
        SupervisorConfig(unit_log_backups=0)
