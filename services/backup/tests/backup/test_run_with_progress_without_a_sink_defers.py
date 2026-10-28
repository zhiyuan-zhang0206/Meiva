from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.native_process.os_platform import LockTimeoutError
from services.backup import dump as backup
from services.backup.layout import BackupTree
from services.backup.tests.support import minted_tree
from services.backup.tests.test_backup import (
    await_backup_lock_holder,
    spawn_backup_lock_holder,
)

_TRY_LOCK = Path(__file__).parent.parent / "child_try_lock.py"


def _run_child_stage(argv: list[str]) -> int:
    mode = argv[1]
    if mode == "slow":
        out = Path(argv[2])
        for _ in range(30):
            with out.open("ab") as fh:
                fh.write(b"x" * 65536)
            time.sleep(0.06)
        return 0
    if mode == "hung":
        marker = Path(argv[2])
        time.sleep(30)
        marker.write_text("finished", encoding="utf-8")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_run_child_stage(sys.argv))


def test_run_with_progress_without_a_sink_is_a_plain_subprocess_run() -> None:
    result = backup.run_with_progress(
        [sys.executable, "--version"], timeout_s=30.0, label="python", progress=None, env={}
    )

    assert result.returncode == 0
    assert result.stdout.startswith(b"Python ")


def test_run_with_progress_heartbeats_while_a_slow_child_runs(tmp_path: Path) -> None:
    out = tmp_path / "slow.dump.partial"
    out.write_bytes(b"")
    lines: list[str] = []

    proc = backup.run_with_progress(
        [sys.executable, __file__, "slow", str(out)],
        timeout_s=120.0,
        label="pg_dump",
        progress=lines.append,
        env={},
        size_path=out,
        interval_s=0.2,
    )

    assert proc.returncode == 0
    assert lines[0] == "pg_dump started (bounded at 2 min)"
    beats = lines[1:]
    assert len(beats) >= 3, f"expected a beat per interval, got {lines}"
    assert all(beat.startswith("pg_dump ") and " MiB written" in beat for beat in beats)
    sizes = [float(beat.split(", ")[1].split(" ")[0]) for beat in beats]
    assert sizes == sorted(sizes), f"byte evidence must only grow: {sizes}"
    assert sizes[-1] > 0


def test_run_with_progress_timeout_still_kills_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "finished"
    lines: list[str] = []
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        backup.run_with_progress(
            [sys.executable, __file__, "hung", str(marker)],
            timeout_s=1.0,
            label="pg_dump",
            progress=lines.append,
            env={},
            interval_s=0.2,
        )

    assert time.monotonic() - started < 10
    assert lines[0].startswith("pg_dump started")
    assert not marker.exists()
    time.sleep(0.3)
    assert not marker.exists(), "the killed child must never complete its work"


def test_run_backup_narrates_both_silent_stages_through_progress(
    tmp_path: Path, db_url: str
) -> None:
    tree = minted_tree(tmp_path)
    lines: list[str] = []

    path = backup.run_backup(
        datetime(2026, 9, 14, 21, 0, tzinfo=UTC),
        tree=tree,
        db_url=db_url,
        keep=7,
        offsite_target=None,
        progress=lines.append,
        env=dict(os.environ),
    )

    assert path.exists()
    minutes = backup.DUMP_TIMEOUT_S / 60
    assert lines == [
        f"pg_dump started (bounded at {minutes:.0f} min)",
        f"backup encryption started (bounded at {minutes:.0f} min)",
    ]


def test_backup_lock_reentrant_same_thread(tmp_path: Path) -> None:
    tree = BackupTree(tmp_path / "meiva-home")

    with backup.backup_lock(tree), backup.backup_lock(tree, timeout_s=0.5):
        pass

    with backup.backup_lock(tree, timeout_s=0.5):
        pass


def test_backup_lock_cross_process_excludes(tmp_path: Path) -> None:
    home = tmp_path / "meiva-home"
    ready = tmp_path / "ready"
    holder = spawn_backup_lock_holder(home, ready, hold_s=2.0)
    try:
        await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with backup.backup_lock(BackupTree(home), timeout_s=30):
            waited = time.monotonic() - started
        assert waited >= 0.4, (
            f"took the backup lock while another process held it (waited {waited:.2f}s)"
        )
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_backup_lock_timeout_expires(tmp_path: Path) -> None:
    home = tmp_path / "meiva-home"
    ready = tmp_path / "ready"
    holder = spawn_backup_lock_holder(home, ready, hold_s=30.0)
    try:
        await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with pytest.raises(LockTimeoutError), backup.backup_lock(BackupTree(home), timeout_s=0.5):
            pytest.fail("took a backup lock another process was holding")
        waited = time.monotonic() - started
        assert 0.3 <= waited < 5
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


# meiva:note
# Another process cannot take the backup lock while either silent stage is running.
def test_run_backup_holds_the_lock_while_its_stages_run(tmp_path: Path, db_url: str) -> None:
    tree = minted_tree(tmp_path)
    probes: list[tuple[str, int]] = []

    def probe(line: str) -> None:
        # meiva:note
        # The probe runs a fixed repository script with the interpreter's own argv, so the
        # subprocess suppression is the harness's.
        taken = subprocess.run(  # noqa: S603
            [sys.executable, str(_TRY_LOCK), str(tree.home)], check=False, timeout=60
        )
        probes.append((line.split(" started", maxsplit=1)[0], taken.returncode))

    backup.run_backup(
        datetime(2026, 8, 8, 3, 0, tzinfo=UTC),
        tree=tree,
        db_url=db_url,
        keep=7,
        offsite_target=None,
        progress=probe,
        env=dict(os.environ),
    )

    assert probes == [("pg_dump", 7), ("backup encryption", 7)]
    with backup.backup_lock(tree, timeout_s=0):
        pass
