# meiva:note
# Stopping the root process: only the exact recorded native birth is ever signalled.
#
# Each test runs a real child that stands in for the root, and records the birth the root's
# status would carry. With no control socket in the home, the client answers "no root", which is
# what a root that has exited looks like.

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.ownership import OwnedProcess
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_with_root

# meiva:note
# The caller block carries this test process's own pid: the value travels through the driver, so
# the assertion is about it reaching the wire, not about the driver reading it.
_PID = os.getpid()

pytestmark = pytest.mark.real_service_readiness_gate

STAND_IN = Path(__file__).with_name("_stand_in_root.py")


# meiva:note
# A running stand-in root, once it has said it is ready.
def stand_in(mode: str) -> subprocess.Popen[bytes]:
    process = subprocess.Popen([sys.executable, "-I", str(STAND_IN), mode], stdout=subprocess.PIPE)
    assert process.stdout is not None
    assert process.stdout.readline().strip() == b"ready"
    return process


@pytest.fixture
def child() -> Iterator[subprocess.Popen[bytes]]:
    process = stand_in("idle")
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def birth_of(process: subprocess.Popen[bytes]) -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(process.pid))


def test_the_root_is_terminated_and_the_stop_returns_once_it_has_exited(
    child: subprocess.Popen[bytes], short_tmp: Path
) -> None:
    root = birth_of(child)

    driver.stop_root_process(
        driver.root_client(short_tmp, environ=os.environ, pid=_PID),
        snapshot_with_root(root),
        timeout_s=10.0,
        helper=None,
        home=short_tmp,
        interval_s=0.01,
        timers=SYSTEM_TIMERS,
    )

    assert child.wait(timeout=5) == -signal.SIGTERM
    assert not root.live()


def test_a_root_that_outlives_the_stop_budget_is_reported(short_tmp: Path) -> None:
    ignores_term = stand_in("ignore-term")
    try:
        with pytest.raises(driver.RootDriverError, match="did not stop within 0s"):
            driver.stop_root_process(
                driver.root_client(short_tmp, environ=os.environ, pid=_PID),
                snapshot_with_root(birth_of(ignores_term)),
                timeout_s=0.0,
                helper=None,
                home=short_tmp,
                interval_s=0.01,
                timers=SYSTEM_TIMERS,
            )
    finally:
        ignores_term.kill()
        ignores_term.wait(timeout=5)


def test_a_birth_that_only_resembles_a_live_process_is_never_signalled(
    child: subprocess.Popen[bytes], short_tmp: Path
) -> None:
    # meiva:note
    # The recorded birth names this pid but another moment: the pid was reused.
    real = birth_of(child)
    ticks = None if real.starttime is None else real.starttime + 1
    reused = replace(real, birth=real.birth + 0.01, starttime=ticks)

    with pytest.raises(driver.RootDriverError, match="no longer live"):
        driver.stop_root_process(
            driver.root_client(short_tmp, environ=os.environ, pid=_PID),
            snapshot_with_root(reused),
            timeout_s=1.0,
            helper=None,
            home=short_tmp,
            interval_s=0.01,
            timers=SYSTEM_TIMERS,
        )

    assert child.poll() is None


def test_a_pid_reused_between_the_liveness_check_and_the_signal_is_never_signalled(
    child: subprocess.Popen[bytes],
) -> None:
    # meiva:note
    # The replacement process appears after `live()` answered yes and before the signal: `signal_pinned`
    # runs the post-liveness pin, fresh capture and birth comparison against the real child.
    real = birth_of(child)
    ticks = None if real.starttime is None else real.starttime + 1
    reused = replace(real, birth=real.birth + 0.01, starttime=ticks)

    with pytest.raises(RuntimeError, match="native identity changed before signal"):
        reused.signal_pinned(signal.SIGTERM)

    assert child.poll() is None


# meiva:note
# Linux identity is the kernel start tick
@pytest.mark.requires_host("linux")
def test_root_signals_use_start_ticks_when_wall_birth_moves(
    child: subprocess.Popen[bytes], short_tmp: Path
) -> None:
    real = birth_of(child)
    wall_clock_stepped = replace(real, birth=real.birth + 3600.0)

    driver.stop_root_process(
        driver.root_client(short_tmp, environ=os.environ, pid=_PID),
        snapshot_with_root(wall_clock_stepped),
        timeout_s=10.0,
        helper=None,
        home=short_tmp,
        interval_s=0.01,
        timers=SYSTEM_TIMERS,
    )

    assert child.wait(timeout=5) == -signal.SIGTERM
