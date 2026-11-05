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
