# meiva:note
# A start is where a pause marker's claim stops being true, so the sweep that runs at the end of
# one must remove the marker on a serving home, keep it on a home that is not serving, and say
# which it did. These tests hold the live fact — the agent host's pid and meiva-root's lock —
# rather than the marker's age, because that is what the sweep reads.
# meiva:link base/deploy/lifecycle/docs/pause-markers.meiva.okf.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from base.deploy.lifecycle import pause_markers

LOCK_HOLDER = Path(__file__).with_name("pause_lock_child.py")
MARKER = "deploy-pause-owner.json"


@pytest.fixture(name="home")
def fixture_home(tmp_path: Path) -> Path:
    (tmp_path / "run").mkdir()
    return tmp_path


def _marker(home: Path, name: str = MARKER, state: str = "paused") -> Path:
    path = home / "run" / name
    path.write_text(json.dumps({"holder": "local-pause:test", "state": state}))
    return path


def _agent_host(home: Path, pid: int) -> None:
    (home / "run" / "agent_host.pid").write_text(f"{pid}\n")


@pytest.fixture(name="root_lock")
def fixture_root_lock(home: Path) -> Iterator[subprocess.Popen[str]]:
    lock = home / "run" / "meiva-root" / "meiva-root.lock"
    lock.parent.mkdir()
    lock.write_text("")
    holder = subprocess.Popen(
        [sys.executable, str(LOCK_HOLDER), str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    yield holder
    if holder.stdin is not None:
        holder.stdin.close()
    holder.wait(timeout=30)


def test_a_serving_home_loses_the_marker_and_the_line_names_it(
    home: Path, root_lock: subprocess.Popen[str]
) -> None:
    marker = _marker(home)
    _agent_host(home, os.getpid())
    assert pause_markers.sweep(home) == [
        "removed pause marker deploy-pause-owner.json (state=paused) — this home is serving"
    ]
    assert not marker.exists()


def test_a_home_that_is_not_serving_keeps_every_marker(
    home: Path, root_lock: subprocess.Popen[str]
) -> None:
    marker = _marker(home)
    scratch = home / "run" / "scratch.lock"
    scratch.write_text("")
    gone = subprocess.Popen(
        [sys.executable, str(LOCK_HOLDER), str(scratch)], stdin=subprocess.DEVNULL
    )
    gone.wait(timeout=30)
    _agent_host(home, gone.pid)
    lines = pause_markers.sweep(home)
    assert marker.exists()
    assert len(lines) == 1 and "kept pause marker deploy-pause-owner.json" in lines[0]
    assert "agent-host=down" in lines[0]


def test_a_marker_stays_when_only_the_agent_host_runs(home: Path) -> None:
    marker = _marker(home)
    _agent_host(home, os.getpid())
    lines = pause_markers.sweep(home)
    assert marker.exists()
    assert "meiva-root=down" in lines[0]


def test_no_marker_produces_no_line(home: Path) -> None:
    assert pause_markers.sweep(home) == []


def test_the_lock_beside_the_marker_goes_with_it(
    home: Path, root_lock: subprocess.Popen[str]
) -> None:
    _marker(home)
    lock = home / "run" / "deploy-pause-owner.lock"
    lock.write_text("")
    _agent_host(home, os.getpid())
    lines = pause_markers.sweep(home)
    assert not lock.exists()
    assert any("removed pause marker deploy-pause-owner.lock" in line for line in lines)
