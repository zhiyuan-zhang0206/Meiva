from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.clock.tests.fakes import system_clock
from base.lm.harness.process import PsutilChildProbe, terminate

SLEEPER = Path(__file__).with_name("sleeper.py")
SLEEP = [sys.executable, str(SLEEPER), "sleep"]
SESSION_LEADER = [sys.executable, str(SLEEPER), "with-child"]
DETACHING_PARENT = [sys.executable, str(SLEEPER), "with-detached-child"]
IGNORING_SIGTERM = [sys.executable, str(SLEEPER), "ignore-sigterm"]


# meiva:note
# Starts `sleeper.py` processes (fixed argv) and kills them when the test ends.
@contextmanager
def started(*commands: list[str], new_session: bool) -> Generator[list[subprocess.Popen[bytes]]]:
    processes = [
        subprocess.Popen(command, start_new_session=new_session)  # noqa: S603
        for command in commands
    ]
    try:
        yield processes
    finally:
        for process in processes:
            process.kill()
            process.wait()


def wait_for_tree(probe: PsutilChildProbe, count: int) -> None:
    deadline = time.monotonic() + 10
    while len(probe.descendants(os.getpid())) < count:
        assert time.monotonic() < deadline, "the children did not start"
        time.sleep(0.02)


def test_a_command_in_a_session_of_its_own_is_a_tool_process_and_a_helper_is_not() -> None:
    probe = PsutilChildProbe()
    with (
        started(SESSION_LEADER, new_session=True) as [tool],
        started(SLEEP, new_session=False) as [helper],
    ):
        wait_for_tree(probe, 3)
        tools = probe.tool_processes(os.getpid())
        assert tool.pid in tools
        assert helper.pid not in tools
        members = set(probe.descendants(tool.pid))
        assert members and members <= set(tools), "what the command started is part of it"


def test_a_process_a_helper_detached_is_not_a_tool_process() -> None:
    probe = PsutilChildProbe()
    with started(DETACHING_PARENT, new_session=False) as [helper]:
        wait_for_tree(probe, 2)
        detached = set(probe.descendants(helper.pid))
        assert detached
        assert probe.tool_processes(os.getpid()).keys().isdisjoint({helper.pid, *detached})


async def test_terminate_ends_a_command_and_what_it_started() -> None:
    probe = PsutilChildProbe()
    with started(SESSION_LEADER, new_session=True) as [tool]:
        wait_for_tree(probe, 2)
        pids = list(probe.tool_processes(os.getpid()))
        assert len(pids) == 2
        stubborn = await terminate(
            probe, pids, grace_seconds=5.0, poll_seconds=0.02, clock=system_clock()
        )
        tool.wait(timeout=5)
        assert stubborn == ()
        assert not any(probe.is_alive(pid) for pid in pids if pid != tool.pid)


# meiva:note
# The process is `sleeper.py` in its ignore-SIGTERM mode (fixed argv).
async def test_terminate_escalates_for_a_process_that_ignores_sigterm() -> None:
    probe = PsutilChildProbe()
    process = subprocess.Popen(  # noqa: S603
        IGNORING_SIGTERM, start_new_session=True, stdout=subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        process.stdout.readline()
        stubborn = await terminate(
            probe, [process.pid], grace_seconds=0.2, poll_seconds=0.02, clock=system_clock()
        )
        process.wait(timeout=5)
        assert stubborn == (process.pid,)
        assert process.returncode == -signal.SIGKILL
    finally:
        process.kill()
        process.wait()
