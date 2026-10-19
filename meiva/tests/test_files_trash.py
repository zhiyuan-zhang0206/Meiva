# meiva:note
# `meiva.files.delete` parks files in the trash; `restore` brings them back.
# Entries are located through the layout instead of a rebuilt path, so the machine
# layer never enters a test. The cross-device branch is drilled on `move_file`,
# which takes its two filesystem primitives as parameters: injecting EXDEV needs no
# mounted second filesystem, and the ordering it pins (copy -> flush -> rename ->
# unlink source) is the property that matters.
# meiva:link meiva/docs/files.meiva.okf.md

import errno
import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

import meiva
from meiva.tests.fixtures.workspace import workspace as workspace

_MACHINE = "trash-test-box"


def _machine_dirs(home: Path) -> list[Path]:
    return sorted(p for p in (home / "trash").glob("*") if p.is_dir())


# meiva:note
# Every parked copy of `origin`, and the single-copy case for tests that expect
# one. The glob walks the machine and day layers instead of rebuilding them.
# meiva:link meiva/docs/files.meiva.okf.md
def _entries(home: Path, origin: Path) -> list[Path]:
    relative = origin.relative_to(origin.anchor)
    return sorted((home / "trash").glob(f"*/*/{relative}"))


def _entry(home: Path, origin: Path) -> Path:
    (entry,) = _entries(home, origin)
    return entry


def test_delete_parks_the_file_under_machine_and_day(workspace: Path, tmp_path: Path) -> None:
    origin = tmp_path / "keep" / "notes.txt"
    origin.parent.mkdir()
    origin.write_text("recoverable")
    origin.chmod(0o640)

    meiva.files.delete(str(origin))

    assert not origin.exists()
    parked = _entry(tmp_path, origin)
    assert parked.read_text() == "recoverable"
    assert parked.stat().st_mode & 0o777 == 0o640
    machine, bucket, *below = parked.relative_to(tmp_path / "trash").parts
    assert machine not in {"", ".", ".."} and "/" not in machine
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", bucket)
    assert below == [*origin.relative_to(origin.anchor).parts]


def test_trash_root_is_owner_only(workspace: Path, tmp_path: Path) -> None:
    origin = tmp_path / "private.txt"
    origin.write_text("x")

    meiva.files.delete(str(origin))

    assert (tmp_path / "trash").stat().st_mode & 0o777 == 0o700


def test_delete_keeps_every_same_day_copy_under_its_own_bucket(
    workspace: Path, tmp_path: Path
) -> None:
    origin = tmp_path / "twice.txt"
    origin.write_text("first")
    meiva.files.delete(str(origin))
    origin.write_text("second")
    meiva.files.delete(str(origin))

    parked = _entries(tmp_path, origin)
    assert sorted(p.read_text() for p in parked) == ["first", "second"]
    assert len({p.parent for p in parked}) == 2


def test_restore_moves_the_file_back(workspace: Path, tmp_path: Path) -> None:
    origin = tmp_path / "back.txt"
    origin.write_text("hello")
    meiva.files.delete(str(origin))

    restored = meiva.files.restore(str(_entry(tmp_path, origin)))

    assert restored == origin
    assert origin.read_text() == "hello"
    assert _entries(tmp_path, origin) == []


def test_restore_never_overwrites_a_new_file_at_the_origin(workspace: Path, tmp_path: Path) -> None:
    origin = tmp_path / "taken.txt"
    origin.write_text("old")
    meiva.files.delete(str(origin))
    origin.write_text("new")

    with pytest.raises(FileExistsError):
        meiva.files.restore(str(_entry(tmp_path, origin)))

    assert origin.read_text() == "new"
    assert _entry(tmp_path, origin).read_text() == "old"


def test_restore_rejects_a_path_outside_the_trash(workspace: Path, tmp_path: Path) -> None:
    outsider = tmp_path / "not-trashed.txt"
    outsider.write_text("x")
    with pytest.raises(ValueError, match="not inside the trash"):
        meiva.files.restore(str(outsider))
    assert outsider.exists()


def test_restore_rejects_a_path_without_a_day_bucket(workspace: Path, tmp_path: Path) -> None:
    stray = tmp_path / "trash" / _MACHINE / "loose.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("x")
    with pytest.raises(ValueError, match="not a trash entry"):
        meiva.files.restore(str(stray))
    assert stray.exists()


def test_delete_parks_a_symlink_as_a_link(workspace: Path, tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("payload")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    meiva.files.delete(str(link))

    assert not os.path.lexists(link)
    parked = _entry(tmp_path, link)
    assert parked.is_symlink()
    assert parked.readlink() == target
    assert target.read_text() == "payload"


def test_move_file_copies_flushes_renames_then_unlinks_across_devices(tmp_path: Path) -> None:
    src = tmp_path / "moved.txt"
    src.write_text("content")
    dst = tmp_path / "parked" / "moved.txt"
    calls: list[Path] = []

    def cross_devices_once(source: Path | str, target: Path | str) -> None:
        calls.append(Path(source))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        Path(source).replace(target)

    meiva.files.move_file(src, dst, replace=cross_devices_once)

    assert not src.exists()
    assert dst.read_text() == "content"
    assert [p.name for p in dst.parent.iterdir()] == ["moved.txt"]


def test_move_file_keeps_the_source_when_the_copy_fails(tmp_path: Path) -> None:
    src = tmp_path / "safe.txt"
    src.write_text("still here")
    dst = tmp_path / "parked" / "safe.txt"

    def cross_devices(source: Path | str, target: Path | str) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def fail_half_way(source: Path | str, target: Path | str, **kwargs: object) -> None:
        Path(target).write_text("half")
        raise OSError(errno.EIO, "copy failed")

    with pytest.raises(OSError):
        meiva.files.move_file(src, dst, replace=cross_devices, copy=fail_half_way)

    assert src.read_text() == "still here"
    assert [_ for _ in dst.parent.iterdir() if _.is_file()] == []


def test_sweep_removes_only_expired_buckets(workspace: Path, tmp_path: Path) -> None:
    today = date(2026, 10, 19)
    expired = tmp_path / "trash" / _MACHINE / "2026-09-01" / "home" / "me" / "old.txt"
    recent = tmp_path / "trash" / _MACHINE / "2026-10-18" / "home" / "me" / "new.txt"
    outside = tmp_path / "outside.txt"
    for path, text in ((expired, "old"), (recent, "new"), (outside, "keep")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    removed = meiva.files.sweep_trash(30, home=tmp_path, today=today)

    assert removed == 1
    assert not expired.exists()
    assert recent.read_text() == "new"
    assert outside.read_text() == "keep"


def test_sweep_does_not_follow_a_symlinked_bucket(workspace: Path, tmp_path: Path) -> None:
    today = date(2026, 10, 19)
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    linked = tmp_path / "trash" / _MACHINE / "2026-01-01"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(precious, target_is_directory=True)

    assert meiva.files.sweep_trash(30, home=tmp_path, today=today) == 0
    assert (precious / "keep.txt").read_text() == "keep"


# meiva:note
# A trash directory that became a symlink is not a trash tree to purge: resolving
# it would hand the purge another directory's contents.
# meiva:link meiva/docs/files.meiva.okf.md
def test_sweep_refuses_a_symlinked_trash_root(workspace: Path, tmp_path: Path) -> None:
    today = date(2026, 10, 19)
    elsewhere = tmp_path / "elsewhere" / "2020-01-01"
    elsewhere.mkdir(parents=True)
    (elsewhere / "keep.txt").write_text("keep")
    (tmp_path / "trash").symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    assert meiva.files.sweep_trash(30, home=tmp_path, today=today) == 0
    assert (elsewhere / "keep.txt").read_text() == "keep"


def test_sweep_is_disabled_by_a_nonpositive_retention(workspace: Path, tmp_path: Path) -> None:
    today = date(2026, 10, 19)
    old = tmp_path / "trash" / _MACHINE / "2020-01-01" / "home" / "me" / "x.txt"
    old.parent.mkdir(parents=True)
    old.write_text("x")

    assert meiva.files.sweep_trash(0, home=tmp_path, today=today) == 0
    assert old.read_text() == "x"


# meiva:note
# A bucket exactly `retention_days` old survives; one day older does not.
# meiva:link meiva/docs/files.meiva.okf.md
def test_sweep_uses_the_default_day_arithmetic(workspace: Path, tmp_path: Path) -> None:
    today = date(2026, 10, 19)
    edge = tmp_path / "trash" / _MACHINE / (today - timedelta(days=30)).isoformat() / "a"
    past = tmp_path / "trash" / _MACHINE / (today - timedelta(days=31)).isoformat() / "b"
    for path in (edge, past):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    assert meiva.files.sweep_trash(30, home=tmp_path, today=today) == 1
    assert edge.exists()
    assert not past.exists()


def test_trash_retention_is_a_configured_default() -> None:
    from base.config.domains.general import GeneralSettings

    field = GeneralSettings.model_fields["trash_retention_days"]
    assert field.default == 30
    assert field.alias == "MEIVA_TRASH_RETENTION_DAYS"
