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


def wait_for_child(probe: PsutilChildProbe, parent: int) -> set[int]:
    deadline = time.monotonic() + 10
    while not (descendants := probe.descendants(parent)):
        assert time.monotonic() < deadline, "the child did not start"
        time.sleep(0.02)
    return set(descendants)


def test_a_command_in_a_session_of_its_own_is_a_tool_process_and_a_helper_is_not() -> None:
    probe = PsutilChildProbe()
    with (
        started(SESSION_LEADER, new_session=True) as [tool],
        started(SLEEP, new_session=False) as [helper],
    ):
        members = wait_for_child(probe, tool.pid)
        tools = probe.tool_processes(os.getpid())
        assert tool.pid in tools
        assert helper.pid not in tools
        assert members <= set(tools), "what the command started is part of it"


def test_a_process_a_helper_detached_is_not_a_tool_process() -> None:
    probe = PsutilChildProbe()
    with started(DETACHING_PARENT, new_session=False) as [helper]:
        detached = wait_for_child(probe, helper.pid)
        assert probe.tool_processes(os.getpid()).keys().isdisjoint({helper.pid, *detached})


async def test_terminate_ends_a_command_and_what_it_started() -> None:
    probe = PsutilChildProbe()
    with started(SESSION_LEADER, new_session=True) as [tool]:
        pids = {tool.pid, *wait_for_child(probe, tool.pid)}
        assert pids <= probe.tool_processes(os.getpid()).keys()
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


# meiva:note
# A residue fence remembers each pid with its start time (task #7074), so the probe must answer the
# same time for the same process and none once it has exited and been reaped.
def test_a_process_keeps_its_start_time_until_it_is_gone() -> None:
    probe = PsutilChildProbe()
    with started(SLEEP, new_session=True) as [process]:
        first = probe.started_at(process.pid)
        assert first is not None
        assert probe.started_at(process.pid) == first
        process.kill()
        _ = process.wait()
        assert probe.started_at(process.pid) is None
