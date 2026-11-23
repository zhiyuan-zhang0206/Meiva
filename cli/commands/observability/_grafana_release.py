# meiva:link deploy/lgtm/README.md

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

from base.paths.scratch import ScratchSpace

_MANIFEST = ".meiva-release.json"
_WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
GRAFANA_STAGING_PREFIX = "meiva-lgtm-tree-"
GRAFANA_BACKUP_PREFIX = "meiva-lgtm-previous-"
GRAFANA_UNPACK_PREFIX = "meiva-lgtm-unpack-"
GRAFANA_DOWNLOAD_PREFIX = "meiva-lgtm-grafana-download-"


def _raise_walk_error(error: OSError) -> None:
    raise error


def _paths(home: Path) -> Iterator[Path]:
    yield home
    for directory, dirs, files in home.walk(on_error=_raise_walk_error):
        yield from (directory / name for name in (*dirs, *files))


def _manifest(home: Path, archive_sha256: str) -> str:
    entries: dict[str, str] = {}
    for path in _paths(home):
        if path == home / _MANIFEST:
            continue
        relative = path.relative_to(home).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            entries[relative] = f"link:{path.readlink()}"
        elif mode & _WRITE_BITS:
            raise ValueError(f"{relative} is writable")
        elif stat.S_ISDIR(mode):
            entries[relative] = f"dir:{stat.S_IMODE(mode):o}"
        elif stat.S_ISREG(mode):
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            entries[relative] = f"file:{stat.S_IMODE(mode):o}:{digest}"
        else:
            raise ValueError(f"{relative} is neither a regular file nor a directory or link")
    return json.dumps({"archive_sha256": archive_sha256, "entries": entries}, sort_keys=True)


def seal_grafana_release(home: Path, *, archive_sha256: str) -> None:
    manifest = home / _MANIFEST
    manifest.touch(exist_ok=False)
    for path in _paths(home):
        if path in (home, manifest):
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISLNK(mode):
            path.chmod(stat.S_IMODE(mode) & ~_WRITE_BITS)
    home.chmod(stat.S_IMODE(home.stat().st_mode) & ~_WRITE_BITS)
    manifest.write_text(_manifest(home, archive_sha256), encoding="utf-8")
    manifest.chmod(0o444)


def grafana_release_problem(home: Path, *, archive_sha256: str) -> str | None:
    try:
        if not stat.S_ISDIR(home.lstat().st_mode):
            return "release home is not a directory"
        manifest = home / _MANIFEST
        mode = manifest.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & _WRITE_BITS:
            return "release manifest is not a read-only file"
        if manifest.read_text(encoding="utf-8") != _manifest(home, archive_sha256):
            return "release tree differs from the pinned archive"
    except FileNotFoundError:
        return "release tree or manifest is missing"
    except (OSError, ValueError) as error:
        return str(error)
    return None


def _remove_work_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    path.chmod(0o700)
    for directory, subdirs, _ in path.walk(on_error=_raise_walk_error):
        for subdir in subdirs:
            (directory / subdir).chmod(0o700)
    shutil.rmtree(path)


def publish_grafana_release(staged: Path, destination: Path, *, previous: Path) -> None:
    backed_up = not destination.is_symlink() and destination.is_dir()
    if backed_up:
        destination.rename(previous)
    else:
        destination.unlink(missing_ok=True)
    try:
        staged.rename(destination)
    except OSError:
        if backed_up:
            previous.rename(destination)
        raise
    if backed_up:
        _remove_work_tree(previous)


def _release_work_paths(home: Path, *prefixes: str) -> Iterator[Path]:
    for prefix in prefixes:
        yield from sorted(home.parent.glob(prefix + "*"))


def recover_grafana_release(home: Path, *, archive_sha256: str, scratch: ScratchSpace) -> bool:
    candidates = tuple(_release_work_paths(home, GRAFANA_STAGING_PREFIX, GRAFANA_BACKUP_PREFIX))
    other_work = tuple(_release_work_paths(home, GRAFANA_DOWNLOAD_PREFIX, GRAFANA_UNPACK_PREFIX))
    if not candidates and not other_work:
        return False
    if grafana_release_problem(home, archive_sha256=archive_sha256) is None:
        return True
    for candidate in candidates:
        if grafana_release_problem(candidate, archive_sha256=archive_sha256) is None:
            with scratch.make(prefix=GRAFANA_BACKUP_PREFIX, parent=home.parent) as previous:
                publish_grafana_release(candidate, home, previous=previous)
            print(f"  · lgtm native: recovered Grafana release from {candidate.name}")
            return True
    return False


def clean_grafana_release_work(home: Path) -> None:
    for path in _release_work_paths(
        home,
        GRAFANA_STAGING_PREFIX,
        GRAFANA_BACKUP_PREFIX,
        GRAFANA_UNPACK_PREFIX,
        GRAFANA_DOWNLOAD_PREFIX,
    ):
        _remove_work_tree(path)
