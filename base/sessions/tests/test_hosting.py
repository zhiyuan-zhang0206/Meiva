from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest

from base.native_process import pid_starttime_ticks
from base.paths import run_dir
from base.sessions.hosting import hosting_supervised_session
from base.sessions.record import SessionRecord


def test_hosting_supervised_session_reads_the_home_it_is_given(unit_home: Path) -> None:
    name = "meiva-test-hosting-current"
    path = run_dir(unit_home) / "sessions" / f"{name}.json"
    SessionRecord(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=pid_starttime_ticks(os.getpid()),
    ).write(path)
    try:
        assert hosting_supervised_session(unit_home) == name
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.requires_host("linux")
def test_hosting_supervised_session_uses_starttime_despite_wall_clock_drift(
    unit_home: Path,
) -> None:
    starttime = pid_starttime_ticks(os.getpid())
    assert starttime is not None
    name = "meiva-test-hosting-starttime"
    path = run_dir(unit_home) / "sessions" / f"{name}.json"
    SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=starttime,
    ).write(path)
    try:
        assert hosting_supervised_session(unit_home) == name
    finally:
        path.unlink(missing_ok=True)
