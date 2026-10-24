"""Backup intermediates: an interrupted dump leaves no plaintext, and every run sweeps closed ones."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from services.backup import dump as backup
from services.backup.artifact.intermediates import sweep_closed_partials
from services.backup.layout import BackupTree
from services.backup.tests.support import held_table_lock, minted_tree, scratch_database


@pytest.fixture
def tree(tmp_path: Path) -> BackupTree:
    return minted_tree(tmp_path / "home")


# meiva:note
# A dump interrupted mid-run (stop, Ctrl-C, timeout) kills and reaps its writer before
# the plaintext partial is removed.
#
# The writer is a real `pg_dump` that cannot take its share lock, so it is still running
# when the first heartbeat reaches the sink and the sink interrupts the run.
def test_interrupted_snapshot_dump_leaves_no_plaintext(tree: BackupTree, db_url: str) -> None:

    def interrupt(line: str) -> None:
        if not line.endswith("min)"):
            raise KeyboardInterrupt

    before = {child.pid for child in psutil.Process().children(recursive=True)}
    with (
        scratch_database(db_url) as url,
        held_table_lock(url),
        pytest.raises(KeyboardInterrupt),
    ):
        backup.run_backup(
            datetime(2026, 8, 8, 10, tzinfo=UTC),
            tree=tree,
            db_url=url,
            keep=7,
            offsite_target=None,
            progress=interrupt,
            progress_interval_s=0.2,
        )

    leftovers = [
        child.name()
        for child in psutil.Process().children(recursive=True)
        if child.pid not in before and "pg_dump" in child.name()
    ]
    assert leftovers == [], "the interrupted run left its pg_dump child running"
    assert not list(tree.managed.glob("*.partial")) and not list(tree.managed.glob(".backup-key-*"))


# meiva:note
# A killed run's orphaned tool may still hold its partial: it stays until that writer
# exits, then the next run removes it. The writer is a fixed script of this directory
# started with the interpreter's own argv, so the subprocess suppression is the harness's.
def test_stale_partial_waits_for_its_orphaned_writer_to_close(tmp_path: Path) -> None:
    held = tmp_path / "meiva-20260926T000000Z.dump.partial"
    closed = tmp_path / "meiva-20260925T000000Z.dump.enc.partial"
    key = tmp_path / ".backup-key-abc"
    for path in (held, closed, key):
        path.write_bytes(b"PLAINTEXT")
    writer = subprocess.Popen(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("child_partial_holder.py")), str(held)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None and writer.stdout.readline() == "open\n"
        sweep_closed_partials(tmp_path)
        assert held.exists() and not closed.exists() and not key.exists()
    finally:
        writer.kill()
        writer.wait(timeout=10)
    sweep_closed_partials(tmp_path)
    assert not held.exists()


# meiva:note
# A killed run's plaintext partial never outlives the next run: the scheduled (staged)
# worker run sweeps the backup directory too, and the same sweep removes key files and
# an abandoned cross-filesystem copy.
def test_every_backup_run_sweeps_closed_intermediates_from_the_backup_directory(
    tree: BackupTree, tmp_path: Path, db_url: str
) -> None:
    tree.managed.mkdir(parents=True, mode=0o700)
    stale = [
        tree.managed / "meiva-20260920T030000Z.dump.partial",
        tree.managed / ".backup-key-stale",
        tree.managed / ".meiva-20260925T030000Z.dump.enc.k3y9.copy",
    ]
    for path in stale:
        path.write_bytes(b"PLAINTEXT")
    staging = tmp_path / "controls" / "artifact"

    artifact = backup.run_backup(
        datetime(2026, 9, 26, 3, tzinfo=UTC),
        tree=tree,
        db_url=db_url,
        keep=7,
        offsite_target=None,
        staging=staging,
    )

    assert artifact.parent == staging
    assert [path.name for path in stale if path.exists()] == []
    assert list(tree.managed.iterdir()) == []
