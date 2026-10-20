from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from base.native_process.exec_domain import ExecProcessDomain
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL

_CHILD = Path(__file__).with_name("_host_death_child.py")
_HOST = Path(__file__).with_name("_host_death_host.py")


async def _read_output(path: Path) -> tuple[int, str]:
    deadline = time.monotonic() + 15
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, "the exec child never started"
        await asyncio.sleep(0.02)
    member, stdin_seen = path.read_text().split(" ", 1)
    return int(member), stdin_seen


async def _assert_gone(pid: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return
        except psutil.NoSuchProcess:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"process {pid} survived the loss of its host")


async def test_closing_the_hosts_control_pipe_kills_the_group(tmp_path: Path) -> None:
    out = tmp_path / "child.out"
    proc, domain = ExecProcessDomain.launch_posix(
        [sys.executable, "-I", str(_CHILD), "leader", str(out)],
        process_control=SYSTEM_PROCESS_CONTROL,
        stdin=subprocess.PIPE,
    )
    try:
        member, stdin_seen = await _read_output(out)
        assert stdin_seen == "''", "user code must see an empty stdin, not the control pipe"
        assert proc.stdin is not None
        proc.stdin.close()

        assert await asyncio.to_thread(proc.wait, 15) == -signal.SIGKILL
        await _assert_gone(member)
    finally:
        domain.kill_and_reap()


async def test_a_killed_host_takes_the_group_with_it(tmp_path: Path) -> None:
    out = tmp_path / "child.out"
    # meiva:note
    # The stand-in host is a separate process, so the test can kill it the way a crash would.
    host = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", str(_HOST), str(out)], stdout=subprocess.PIPE, text=True
    )
    assert host.stdout is not None
    leader = int(host.stdout.readline())
    try:
        member, _ = await _read_output(out)
        host.kill()
        host.wait(timeout=10)

        await _assert_gone(leader)
        await _assert_gone(member)
    finally:
        host.kill()
        host.wait(timeout=10)
        for pid in (leader,):
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()
