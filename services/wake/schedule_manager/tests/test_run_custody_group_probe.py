# meiva:note
# How a run's numeric process group is judged empty when `killpg(pgid, 0)` answers EPERM.
# Darwin answers EPERM for a group holding only unreaped zombies as well as for one whose live
# members the caller may not signal. The zombie-only case is the one a restarted manager meets
# right after a run's leader exits, and must settle as empty; every other EPERM stays unproven.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest

from base.native_process.ownership import LiveReading, OwnedProcess
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL, ProcessControl
from base.native_process.tests.fake_process_control import FakeProcessControl
from base.sessions.posixproc import GroupScan
from services.wake.schedule_manager.run_custody import (
    SYSTEM_PROCESS_IDENTITY_CONTROL,
    GroupLiveness,
    ProcessIdentityControl,
    RunClaim,
    observe_run_group,
)

_EPERM = PermissionError(1, "Operation not permitted")
_CHILD = Path(__file__).with_name("group_probe_child.py")


# meiva:note
# Identity control for a claim whose captured processes are all gone.
class _GonePeople:
    def __init__(self, scan: GroupScan) -> None:
        self._scan = scan
        self.scanned: list[int] = []

    def read_live(self, identity: OwnedProcess) -> LiveReading | None:
        del identity
        return None

    def signal_pinned(self, identity: OwnedProcess, signum: int) -> bool:
        raise AssertionError(f"nothing may be signalled: {identity.pid} {signum}")

    def scan_group(self, pgid: int, *, process_control: ProcessControl) -> GroupScan:
        del process_control
        self.scanned.append(pgid)
        return self._scan


def _claim(leader: OwnedProcess) -> RunClaim:
    return RunClaim(
        schedule_id=1,
        run_id=2,
        leader=leader,
        pgid=leader.pid,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def _refusing_killpg(pgid: int, error: OSError) -> FakeProcessControl:
    control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    control.refuse(pgid, error)
    return control


def _observe(
    claim: RunClaim, control: ProcessControl, identities: ProcessIdentityControl
) -> tuple[GroupLiveness, str]:
    observed = observe_run_group(claim, process_control=control, identity_control=identities)
    return observed.group, observed.detail


@contextmanager
def _own_group_child(mode: str) -> Generator[subprocess.Popen[str]]:
    # meiva:note
    # Spawns the test-local helper module in isolated mode in its own session and process group.
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", str(_CHILD), mode],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def _wait_until(predicate: object) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_a_zombie_only_group_refused_with_eperm_is_empty() -> None:
    with _own_group_child("noop") as child:
        leader = OwnedProcess.capture(psutil.Process(child.pid))
        # meiva:note
        # The leader has exited but is not reaped: it stays in its group as a zombie.
        _wait_until(lambda: psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE)
        control = _refusing_killpg(child.pid, _EPERM)

        group, detail = _observe(_claim(leader), control, SYSTEM_PROCESS_IDENTITY_CONTROL)

    assert group is GroupLiveness.EMPTY
    assert "only unreaped zombies" in detail
    assert [(sent.target, sent.signum) for sent in control.signals] == [(child.pid, 0)]


def test_a_live_member_the_caller_cannot_signal_stays_unproven() -> None:
    # meiva:note
    # The leader forks a sleeper into its group and exits; once reaped, only the sleeper holds
    # the numeric group, standing in for another UID's process that EPERM would shield.
    with _own_group_child("fork_sleeper") as child:
        assert child.stdout is not None
        sleeper = int(child.stdout.readline())
        try:
            leader = OwnedProcess.capture(psutil.Process(child.pid))
            child.wait()
            control = _refusing_killpg(child.pid, _EPERM)

            group, detail = _observe(_claim(leader), control, SYSTEM_PROCESS_IDENTITY_CONTROL)
        finally:
            os.kill(sleeper, signal.SIGKILL)

    assert group is GroupLiveness.UNKNOWN
    assert "live member(s) this process cannot signal" in detail


@pytest.mark.parametrize(
    ("scan", "wanted", "phrase"),
    [
        (GroupScan(live=0, unreadable=0), GroupLiveness.EMPTY, "only unreaped zombies"),
        (GroupScan(live=2, unreadable=0), GroupLiveness.UNKNOWN, "2 live member(s)"),
        (GroupScan(live=0, unreadable=3), GroupLiveness.UNKNOWN, "3 process(es) could not be"),
        (GroupScan(live=1, unreadable=3), GroupLiveness.UNKNOWN, "1 live member(s)"),
    ],
)
def test_eperm_is_settled_by_what_the_process_table_scan_saw(
    scan: GroupScan, wanted: GroupLiveness, phrase: str
) -> None:
    leader = OwnedProcess(pid=4100, birth=1.0, starttime=10)
    identities = _GonePeople(scan)

    group, detail = _observe(_claim(leader), _refusing_killpg(4100, _EPERM), identities)

    assert group is wanted
    assert phrase in detail
    assert identities.scanned == [4100]


def test_a_group_the_kernel_reports_absent_is_empty_without_a_table_scan() -> None:
    leader = OwnedProcess(pid=4100, birth=1.0, starttime=10)
    identities = _GonePeople(GroupScan(live=1, unreadable=0))

    group, _ = _observe(
        _claim(leader), _refusing_killpg(4100, ProcessLookupError(3, "No such process")), identities
    )

    assert group is GroupLiveness.EMPTY
    assert identities.scanned == []


def test_a_failure_other_than_eperm_is_never_read_as_empty() -> None:
    leader = OwnedProcess(pid=4100, birth=1.0, starttime=10)
    identities = _GonePeople(GroupScan(live=0, unreadable=0))

    group, detail = _observe(
        _claim(leader), _refusing_killpg(4100, OSError(5, "Input/output error")), identities
    )

    assert group is GroupLiveness.UNKNOWN
    assert "is unprovable" in detail
    assert identities.scanned == []
