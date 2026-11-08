from __future__ import annotations

import logging
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.clock.tests.fakes import clock_at
from base.clock.tests.seeded_rng import seeded_rng
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.paths import meiva_home
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty import allocation_freeze
from base.sessions.pty.tests.pty_service import pty_service as pty_service

pytestmark = pytest.mark.usefixtures("pty_service")
WRITER = AtomicWriter(SYSTEM_FS)
NOW = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)


def test_freeze_lives_in_the_home_and_records_operator_generation() -> None:
    home = meiva_home()
    assert allocation_freeze.state_path(home) == home / "pty-allocation-freeze.json"
    assert allocation_freeze.lock_path(home) == home / "pty-allocation.lock"

    frozen = allocation_freeze.freeze(
        holder="operator-1818",
        reason="bounded cleanup",
        writer=WRITER,
        home=home,
        clock=clock_at(NOW),
        rng=seeded_rng(5),
    )

    assert frozen.status == "frozen"
    assert frozen.generation is not None
    assert frozen.holder == "operator-1818"
    assert frozen.reason == "bounded cleanup"
    assert frozen.created_at is not None
    assert allocation_freeze.read(home=home) == frozen
    assert allocation_freeze.resume(frozen.generation, home=home)


def test_freeze_rejects_a_second_call_while_active() -> None:
    home = meiva_home()
    frozen = allocation_freeze.freeze(
        holder="operator-1818",
        reason="bounded cleanup",
        writer=WRITER,
        home=home,
        clock=clock_at(NOW),
        rng=seeded_rng(5),
    )
    with pytest.raises(allocation_freeze.PtyAllocationAlreadyFrozenError):
        allocation_freeze.freeze(
            holder="operator-other",
            reason="conflicting cleanup",
            writer=WRITER,
            home=home,
            clock=clock_at(NOW),
            rng=seeded_rng(5),
        )
    assert frozen.generation is not None
    assert allocation_freeze.resume(frozen.generation, home=home)


def test_resume_is_generation_scoped_and_never_clears_a_newer_owner() -> None:
    home = meiva_home()
    rng = seeded_rng(6)
    first = allocation_freeze.freeze(
        holder="first",
        reason="first cleanup",
        writer=WRITER,
        home=home,
        clock=clock_at(NOW),
        rng=rng,
    )
    assert first.generation is not None

    assert not allocation_freeze.resume("stale-generation", writer=WRITER, home=home)
    assert allocation_freeze.read(home=home) == first
    assert allocation_freeze.resume(first.generation, writer=WRITER, home=home)
    active = allocation_freeze.read(home=home)
    assert active.status == "inactive"
    assert active.generation == first.generation
    assert allocation_freeze.current_generation(home=home) == first.generation

    second = allocation_freeze.freeze(
        holder="second",
        reason="second cleanup",
        writer=WRITER,
        home=home,
        clock=clock_at(NOW),
        rng=rng,
    )
    assert second.generation is not None and second.generation != first.generation
    assert not allocation_freeze.resume(first.generation, writer=WRITER, home=home)
    assert allocation_freeze.read(home=home) == second
    assert allocation_freeze.resume(second.generation, writer=WRITER, home=home)


def test_write_atomic_propagates_a_real_target_directory_failure(tmp_path: Path) -> None:
    folder = tmp_path / "scratch"
    folder.mkdir()
    path = folder / "state.json"
    path.mkdir()
    retained = path / "existing"
    retained.write_bytes(b"old")

    with pytest.raises(OSError):
        allocation_freeze.write_atomic(path, {"z": 1, "a": "value"}, writer=WRITER)
    assert retained.read_bytes() == b"old"


def test_write_atomic_survives_directory_sync_failure_and_cleans_temps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    folder = tmp_path / "scratch"
    folder.mkdir()
    path = folder / "state.json"
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
    assert sorted(folder.iterdir()) == [path]
    assert path.stat().st_mode & 0o777 == 0o600
    assert "directory fsync failed after marker commit" in caplog.text
