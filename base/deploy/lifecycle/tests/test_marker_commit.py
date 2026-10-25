# meiva:note
# What the two marker writers of `base/deploy/lifecycle` promise on disk: the marker commits even
# when the directory sync after it fails, no temporary file survives, and concurrent writers
# publish complete snapshots only. Moved here from the observability command tests, which never
# exercised code under that package.

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from base.deploy.lifecycle import home_lifecycle_locks, start_serving
from base.host.atomic_io import SYSTEM_FS, AtomicWriter

_START_GENERATION = "00000000-0000-4000-8000-000000000001"
_WRITER = AtomicWriter(SYSTEM_FS)


def _marker_write(case: str, path: Path, home: Path, *, writer: AtomicWriter) -> None:
    payload: dict[str, object] = {"z": 1, "a": "value"}
    if case == "start":
        start_serving.write_state(
            home,
            start_serving.ServingState(state="starting", generation=_START_GENERATION),
            writer=writer,
        )
    elif case == "locks":
        home_lifecycle_locks.write_atomic(path, payload, writer=writer)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize("case", ["start", "locks"])
def test_marker_commit_survives_directory_sync_failure_and_cleans_temps(
    case: str, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    path = start_serving.state_path(home) if case == "start" else tmp_path / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"old")

    def sync_files_only(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failure")
        os.fsync(fd)

    _marker_write(
        case,
        path,
        home,
        writer=AtomicWriter(replace(SYSTEM_FS, fsync=sync_files_only)),
    )
    raw = path.read_bytes()
    if case == "start":
        assert raw == (
            b'{"schema_version":2,"state":"starting","generation":"'
            + _START_GENERATION.encode()
            + b'","birth":null}'
        )
    else:
        assert raw == b'{"a":"value","z":1}'
    assert sorted(path.parent.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600


def test_concurrent_marker_writers_publish_complete_snapshots(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    def write(index: int) -> None:
        home_lifecycle_locks.write_atomic(
            path, {"index": index, "data": "x" * 4096}, writer=_WRITER
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))
    result = json.loads(path.read_bytes())
    assert result in ({"index": index, "data": "x" * 4096} for index in range(24))
    assert sorted(tmp_path.iterdir()) == [path]
