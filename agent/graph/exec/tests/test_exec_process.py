from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

import psutil
import pytest

from agent.graph.exec import _subprocess
from agent.graph.exec._result import ExecDone
from base.native_process.exec_domain import ExecProcessDomain
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.native_process.tests.fake_process_control import FakeProcessControl
from base.sessions.posixproc import process_group_has_live_members
from meiva.tests.fixtures.pin_agent import exec_context

_AGENT_ID = 424242

_GROUP_CHILD = Path(__file__).with_name("_group_child.py")


async def _wait_for(path: Path, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, f"{path} was never written"
        await asyncio.sleep(0.02)
    return path.read_text()


async def _assert_gone(pid: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} is still alive")


async def test_kill_and_reap_ends_the_whole_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    proc, domain = ExecProcessDomain.launch_posix(
        [sys.executable, "-I", str(_GROUP_CHILD), str(pid_file)],
        process_control=SYSTEM_PROCESS_CONTROL,
    )
    member = int(await _wait_for(pid_file))

    assert domain.kill_and_reap() == -signal.SIGKILL

    await _assert_gone(member)
    assert not process_group_has_live_members(proc.pid)


def test_a_reaped_leader_is_never_signalled() -> None:
    process_control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    proc, domain = ExecProcessDomain.launch_posix(["true"], process_control=process_control)
    assert proc.wait(timeout=10) == 0

    domain.signal(signal.SIGKILL)

    assert domain.kill_and_reap() == 0
    assert process_control.signals == []


def test_launch_owns_process_grouping() -> None:
    with pytest.raises(ValueError, match="process grouping"):
        ExecProcessDomain.launch_posix(
            ["true"],
            process_control=SYSTEM_PROCESS_CONTROL,
            start_new_session=False,
        )


async def test_a_run_ends_with_its_group_dead_and_its_files_removed(tmp_path: Path) -> None:
    pid_file = tmp_path / "member.pid"
    code = (
        "import pathlib, subprocess, sys\n"
        "member = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(member.pid))\n"
        "print('leader done')\n"
    )

    result, payload = await _subprocess.run_in_subprocess(
        code,
        exec_context(_AGENT_ID),
        asyncio.Event(),
        30.0,
        exec_dir=tmp_path / "exec",
        accumulation_max_chars=1_000_000,
    )

    assert isinstance(result, ExecDone), result.output
    assert payload is not None and payload.kind == "done"
    await _assert_gone(int(pid_file.read_text()))
    assert not list((tmp_path / "exec" / str(_AGENT_ID)).iterdir())


async def test_a_detached_output_holder_cannot_hold_the_run_past_the_drain_bound(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "detached.pid"
    code = (
        "import pathlib, subprocess, sys\n"
        "detached = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(detached.pid))\n"
        "print('before detach')\n"
    )
    try:
        started = time.monotonic()
        result, _payload = await _subprocess.run_in_subprocess(
            code,
            exec_context(_AGENT_ID),
            asyncio.Event(),
            30.0,
            exec_dir=tmp_path / "exec",
            accumulation_max_chars=1_000_000,
        )

        assert isinstance(result, ExecDone), result.output
        assert "before detach" in result.output
        assert time.monotonic() - started < 25
    finally:
        if pid_file.exists():
            psutil.Process(int(pid_file.read_text())).kill()
