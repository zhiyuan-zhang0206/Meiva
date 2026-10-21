# meiva:note
# A group signal can be refused while its leader is exiting: measured 2026-10-21 — the pid read
# `idle` at the refusal and was gone microseconds later — and the refusal broke a full test run as
# `ServiceError: internal error in close_all`. A refusal on a birth that disappears right after is
# the disappearance this module already treats as normal; a birth that outlives the short wait is
# a real permission problem and must stay loud.
# meiva:link base/sessions/pty/docs/pty_sessions/pty_sessions.meiva.okf.md

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterator

import psutil
import pytest

from base.native_process.ownership import OwnedProcess
from base.sessions.pty import process_groups


@pytest.fixture
def leader() -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield process
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _identity(process: subprocess.Popen[bytes]) -> OwnedProcess:
    return OwnedProcess.capture(psutil.Process(process.pid))


def test_a_refusal_that_would_kill_a_living_group_stays_loud(
    leader: subprocess.Popen[bytes],
) -> None:
    identity = _identity(leader)

    def refused(_group: int, _signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    with pytest.raises(PermissionError, match="denied for group"):
        process_groups.signal((identity,), signal.SIGKILL, kill_group=refused)


def test_a_refusal_on_a_birth_that_disappears_is_a_disappearance(
    leader: subprocess.Popen[bytes],
) -> None:
    identity = _identity(leader)

    def refused_and_gone(_group: int, _signum: int) -> None:
        os.kill(leader.pid, signal.SIGKILL)
        leader.wait(timeout=10)
        raise PermissionError(1, "Operation not permitted")

    process_groups.signal((identity,), signal.SIGKILL, kill_group=refused_and_gone)
    assert not identity.live()
