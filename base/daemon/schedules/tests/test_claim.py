from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from base.daemon.schedules.claim import (
    RunnerLiveness,
    claim_path,
    clear_claim,
    read_runner_liveness,
    write_claim,
)
from base.deploy.schedule_timing import SCHEDULE_LAUNCH_GRACE_S
from base.native_process.ownership import OwnedProcess


def _sleeper() -> subprocess.Popen[bytes]:
    # meiva:note
    # A real second process: the reading this module makes is about the process table, and a fake
    # one would test the fake.
    return subprocess.Popen(["/bin/sleep", "60"])


def test_a_recorded_live_process_reads_alive(tmp_path: Path) -> None:
    proc = _sleeper()
    try:
        path = write_claim(tmp_path, 7, pid=proc.pid)
        assert path == claim_path(tmp_path, 7)
        reading = read_runner_liveness(tmp_path, 7)
    finally:
        proc.kill()
        proc.wait()
    assert reading.liveness is RunnerLiveness.ALIVE
    assert reading.claim is not None and reading.claim.pid == proc.pid


def test_the_same_pid_after_its_process_died_reads_dead(tmp_path: Path) -> None:
    proc = _sleeper()
    write_claim(tmp_path, 7, pid=proc.pid)
    proc.kill()
    proc.wait()

    reading = read_runner_liveness(tmp_path, 7)

    assert reading.liveness is RunnerLiveness.DEAD
    assert reading.detail == f"pid {proc.pid} is gone"


def test_a_reused_pid_reads_dead(tmp_path: Path) -> None:
    # meiva:note
    # Pid reuse, built from two real processes: the record carries the child's native identity
    # but this process's pid. Whichever field the platform decides with -- the Linux start tick
    # or the native birth elsewhere -- belongs to a different process than the pid now does.
    child = _sleeper()
    try:
        born_as = OwnedProcess.capture(psutil.Process(child.pid))
        record: dict[str, object] = {
            "schedule_id": 7,
            "pid": os.getpid(),
            "birth": born_as.birth,
            "starttime": born_as.starttime,
        }
        path = claim_path(tmp_path, 7)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
    finally:
        child.kill()
        child.wait()

    reading = read_runner_liveness(tmp_path, 7)

    assert reading.liveness is RunnerLiveness.DEAD
    assert reading.detail == f"pid {os.getpid()} now belongs to a different process"


def test_a_record_left_by_a_process_that_became_a_zombie_reads_dead(tmp_path: Path) -> None:
    proc = _sleeper()
    write_claim(tmp_path, 7, pid=proc.pid)
    proc.kill()
    # meiva:note
    # The pid stays in the process table until it is reaped; a runner's parent is the session's
    # login shell, which may not reap for hours (that is the production shape).
    deadline = time.monotonic() + 10
    reading = read_runner_liveness(tmp_path, 7)
    while reading.liveness is not RunnerLiveness.DEAD and time.monotonic() < deadline:
        time.sleep(0.05)
        reading = read_runner_liveness(tmp_path, 7)
    proc.wait()

    assert reading.liveness is RunnerLiveness.DEAD
    assert "zombie" in reading.detail


def test_a_record_from_the_previous_runner_is_not_this_sessions_death(tmp_path: Path) -> None:
    # meiva:note
    # The production shape of 2026-11-06: the manager reaps a runner with a forced kill, so that
    # process never clears its record; the new session starts and its runner needs a second or two
    # before it writes its own. Judged without the launch instant, that leftover record reads as a
    # death a quarter of a second after a successful launch, and the schedule is marked lost with
    # automatic relaunch disabled.
    previous = _sleeper()
    write_claim(tmp_path, 7, pid=previous.pid)
    previous.kill()
    previous.wait()
    launched_at = time.time()

    reading = read_runner_liveness(tmp_path, 7, launched_at=launched_at, now=launched_at + 1.0)

    assert reading.liveness is RunnerLiveness.UNKNOWN
    assert "previous runner's record" in reading.detail


def test_that_record_reads_dead_once_the_launch_grace_has_passed(tmp_path: Path) -> None:
    # meiva:note
    # The grace is bounded on purpose: a session whose runner never came up must still be named as
    # dead, or the fix trades a false "lost" for a schedule that is never judged at all.
    previous = _sleeper()
    write_claim(tmp_path, 7, pid=previous.pid)
    previous.kill()
    previous.wait()
    launched_at = time.time()

    reading = read_runner_liveness(
        tmp_path, 7, launched_at=launched_at, now=launched_at + SCHEDULE_LAUNCH_GRACE_S + 1
    )

    assert reading.liveness is RunnerLiveness.DEAD
    assert reading.detail == f"pid {previous.pid} is gone"


def test_a_live_record_predating_the_launch_is_still_alive(tmp_path: Path) -> None:
    # meiva:note
    # Adoption is the other side: the manager may take over a session an earlier unit started, and
    # a process that is provably the recorded one is alive whatever the launch instant says.
    proc = _sleeper()
    try:
        write_claim(tmp_path, 7, pid=proc.pid)
        reading = read_runner_liveness(tmp_path, 7, launched_at=time.time() + 60)
    finally:
        proc.kill()
        proc.wait()

    assert reading.liveness is RunnerLiveness.ALIVE


def test_no_record_reads_unknown(tmp_path: Path) -> None:
    reading = read_runner_liveness(tmp_path, 7)

    assert reading.liveness is RunnerLiveness.UNKNOWN
    assert reading.claim is None


def test_an_unreadable_record_reads_unknown(tmp_path: Path) -> None:
    path = claim_path(tmp_path, 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert read_runner_liveness(tmp_path, 7).liveness is RunnerLiveness.UNKNOWN


def test_a_record_for_another_schedule_is_not_this_one(tmp_path: Path) -> None:
    path = claim_path(tmp_path, 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schedule_id": 8, "pid": os.getpid(), "birth": 1.0}))

    assert read_runner_liveness(tmp_path, 7).liveness is RunnerLiveness.UNKNOWN


def test_a_pid_that_is_gone_writes_no_record(tmp_path: Path) -> None:
    # meiva:note
    # The writer cannot describe a process that is already gone, and a schedule must not fail to
    # start because of it: it runs unrecorded, which reads as UNKNOWN and is never acted on.
    proc = _sleeper()
    proc.kill()
    proc.wait()

    assert write_claim(tmp_path, 7, pid=proc.pid) is None
    assert not claim_path(tmp_path, 7).exists()


def test_clearing_removes_the_record(tmp_path: Path) -> None:
    proc = _sleeper()
    try:
        path = write_claim(tmp_path, 7, pid=proc.pid)
        assert path is not None
        clear_claim(tmp_path, 7)
        assert not claim_path(tmp_path, 7).exists()
    finally:
        proc.kill()
        proc.wait()


def test_clearing_an_absent_record_is_not_an_error(tmp_path: Path) -> None:
    clear_claim(tmp_path, 7)


@pytest.mark.parametrize("payload", [b"[]", b'"text"', b"{}"])
def test_a_record_of_the_wrong_shape_reads_unknown(tmp_path: Path, payload: bytes) -> None:
    path = claim_path(tmp_path, 7)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    assert read_runner_liveness(tmp_path, 7).liveness is RunnerLiveness.UNKNOWN
