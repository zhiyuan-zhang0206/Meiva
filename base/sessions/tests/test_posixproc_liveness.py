"""Unit-level tests for base.sessions.posixproc's liveness primitives.

Stubbed native reads cover zombie handling; a retained real child also checks
the public process-group observation before exit and while awaiting reap.
"""

from __future__ import annotations

import subprocess
import sys
import time
import types
from pathlib import Path

import psutil
import pytest

from base.native_process.tests.fake_process_control import FakeProcessControl
from base.sessions import posixproc

_CHILD = Path(__file__).with_name("process_evidence.py")


def test_process_is_live_false_for_zombie() -> None:
    """process_is_live counts a zombie as dead awaiting its parent's reap. The
    graceful verdict must not wait on init/launchd reap latency (macmini
    failure on #1301; #1303 class)."""
    proc = types.SimpleNamespace()
    proc.is_running = lambda: True
    proc.status = lambda: psutil.STATUS_ZOMBIE
    assert posixproc.process_is_live(proc) is False  # type: ignore[arg-type]


def test_group_observation_ignores_zombie_members() -> None:
    """A group whose only occupants are zombies is empty for the graceful
    verdict — killpg(pgid, 0) would still succeed on it, so the fast probe is
    followed by a member walk that exempts zombies."""
    # meiva:note
    # SimpleNamespace stand-in supplies only pid and status() for the member walk.
    zombie = types.SimpleNamespace(pid=424242, status=lambda: psutil.STATUS_ZOMBIE)
    control = FakeProcessControl(groups={424242: 999})
    assert (
        posixproc.process_group_has_live_members(
            999,
            process_control=control,
            processes=[zombie],  # pyright: ignore[reportArgumentType]
        )
        is False
    )


def test_group_observation_detects_live_member() -> None:
    """A live member keeps the group occupied."""
    # meiva:note
    # SimpleNamespace stand-in supplies only pid and status() for the member walk.
    live = types.SimpleNamespace(pid=1, status=lambda: psutil.STATUS_RUNNING)
    control = FakeProcessControl(groups={1: 999})
    assert (
        posixproc.process_group_has_live_members(
            999,
            process_control=control,
            processes=[live],  # pyright: ignore[reportArgumentType]
        )
        is True
    )


@pytest.mark.parametrize("pgid", [None, 0, -1])
def test_unknown_group_has_no_observed_live_member(pgid: int | None) -> None:
    assert posixproc.process_group_has_live_members(pgid) is False


def test_group_observation_of_live_then_unreaped_child() -> None:
    """An exited leader awaiting reap is not a live group member."""
    # meiva:note
    # Spawns the test-local helper module in isolated mode as a group leader.
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", str(_CHILD), "read_stdin_byte"],
        start_new_session=True,
        stdin=subprocess.PIPE,
    ) as child:
        try:
            assert posixproc.process_group_has_live_members(child.pid) is True
            assert child.stdin is not None
            child.stdin.close()
            process = psutil.Process(child.pid)
            deadline = time.monotonic() + 5
            while process.status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline, "test child did not exit before its deadline"
                time.sleep(0.01)
            assert child.returncode is None
            assert posixproc.process_group_has_live_members(child.pid) is False
        finally:
            child.kill()
            child.wait(timeout=5)


def _member(pid: int, status: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(pid=pid, status=lambda: status)


def test_group_scan_counts_live_members_and_skips_zombies_and_other_groups() -> None:
    control = FakeProcessControl(groups={1: 999, 2: 999, 3: 999, 4: 555})
    # meiva:note
    # SimpleNamespace stand-in supplies only pid and status() for the table walk.
    processes = [
        _member(1, psutil.STATUS_RUNNING),
        _member(2, psutil.STATUS_ZOMBIE),
        _member(3, psutil.STATUS_SLEEPING),
        _member(4, psutil.STATUS_RUNNING),
    ]
    scan = posixproc.scan_process_group(
        999,
        process_control=control,
        processes=processes,  # pyright: ignore[reportArgumentType]
    )
    assert scan == posixproc.GroupScan(live=2, unreadable=0)


def test_group_scan_tells_an_uninspectable_process_from_an_exited_one() -> None:
    def denied() -> str:
        raise psutil.AccessDenied(pid=7)

    def gone() -> str:
        raise psutil.NoSuchProcess(pid=8)

    control = FakeProcessControl(groups={7: 999, 8: 999})
    # meiva:note
    # SimpleNamespace stand-ins supply only pid and status(); pid 9 has no group on record so its
    # read raises ProcessLookupError, as an exited pid does.
    processes = [
        types.SimpleNamespace(pid=7, status=denied),
        types.SimpleNamespace(pid=8, status=gone),
        types.SimpleNamespace(pid=9, status=lambda: psutil.STATUS_RUNNING),
    ]
    scan = posixproc.scan_process_group(
        999,
        process_control=control,
        processes=processes,  # pyright: ignore[reportArgumentType]
    )
    assert scan == posixproc.GroupScan(live=0, unreadable=1)
