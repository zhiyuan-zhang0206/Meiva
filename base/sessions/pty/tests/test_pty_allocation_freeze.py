from __future__ import annotations

import logging
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.sessions.pty import allocation_freeze

WRITER = AtomicWriter(SYSTEM_FS)


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_freeze_lives_in_the_home_and_records_operator_generation(isolated_home: Path) -> None:
    assert (
        allocation_freeze.state_path(isolated_home) == isolated_home / "pty-allocation-freeze.json"
    )
    assert allocation_freeze.lock_path(isolated_home) == isolated_home / "pty-allocation.lock"

    frozen = allocation_freeze.freeze(
        holder="operator-1818", reason="bounded cleanup", writer=WRITER, home=isolated_home
    )

    assert frozen.status == "frozen"
    assert frozen.generation
    assert frozen.holder == "operator-1818"
    assert frozen.reason == "bounded cleanup"
    assert frozen.created_at is not None
    assert allocation_freeze.read(home=isolated_home) == frozen


def test_resume_is_generation_scoped_and_never_clears_a_newer_owner(isolated_home: Path) -> None:
    first = allocation_freeze.freeze(
        holder="first", reason="first cleanup", writer=WRITER, home=isolated_home
    )
    assert first.generation is not None

    assert not allocation_freeze.resume("stale-generation", writer=WRITER, home=isolated_home)
    assert allocation_freeze.read(home=isolated_home) == first
    assert allocation_freeze.resume(first.generation, writer=WRITER, home=isolated_home)
    active = allocation_freeze.read(home=isolated_home)
    assert active.status == "inactive"
    assert active.generation == first.generation
    assert allocation_freeze.current_generation(home=isolated_home) == first.generation

    second = allocation_freeze.freeze(
        holder="second", reason="second cleanup", writer=WRITER, home=isolated_home
    )
    assert second.generation is not None and second.generation != first.generation
    assert not allocation_freeze.resume(first.generation, writer=WRITER, home=isolated_home)
    assert allocation_freeze.read(home=isolated_home) == second


def test_write_atomic_propagates_a_real_target_directory_failure(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.mkdir()
    retained = path / "existing"
    retained.write_bytes(b"old")

    with pytest.raises(OSError):
        allocation_freeze.write_atomic(path, {"z": 1, "a": "value"}, writer=WRITER)
    assert retained.read_bytes() == b"old"


def test_write_atomic_survives_directory_sync_failure_and_cleans_temps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b"old")

    def sync_files_only(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failure")
        os.fsync(fd)

    with caplog.at_level(logging.WARNING, logger="base.sessions.pty.allocation_freeze"):
        allocation_freeze.write_atomic(
            path,
            {"z": 1, "a": "value"},
            writer=AtomicWriter(replace(SYSTEM_FS, fsync=sync_files_only)),
        )
    assert path.read_bytes() == b'{"a":"value","z":1}'
    assert sorted(tmp_path.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "directory fsync failed after marker commit" in caplog.text
