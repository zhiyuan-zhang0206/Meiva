# meiva:note
# The disk side of a sealed release tree: observe every path with `lstat` (and, when asked, its
# bytes), seal and unseal through the platform's own `chmod -R` and `chflags -R`, write and read
# the manifest beside the tree, and verify a tree against it. The entry model, the manifest form
# and the comparison are decided in `sealed_plan.py`.
#
# Sealing is a containment layer against careless writes by the operator's own uid, not a
# boundary: that uid can unseal. Verification is what makes an unsealing visible, because both
# commands move every touched path's ctime.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path
from typing import assert_never

from base.deploy.sealed_release.sealed_plan import (
    Entry,
    EntryKind,
    Manifest,
    differences,
    manifest_path,
)
from base.host.atomic_io import write_text_atomic
from base.host.system.hostport import Host

_SEAL_TIMEOUT_S = 600.0
_STABLE = ("st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")


class SealedTreeError(RuntimeError):
    pass


# meiva:note
# Every path under `root`, the root itself as ".", keyed by tree-relative POSIX path. Symlinks are
# recorded, never followed: a release's `.venv/bin/python` points at the uv interpreter outside the
# tree, which is not the release's to seal. Keys are built as strings beside `os.scandir`: on an
# 82k-entry release, `Path.relative_to` alone cost 42 s of a 63 s profiled walk, and the walk is
# what every `lstat` verification pays. A directory that cannot be listed raises rather than
# becoming a quieter "missing" finding.
def observe(root: Path, *, content: bool) -> dict[str, Entry]:
    entries = {".": _entry(root, root.lstat(), content=content)}
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as listing:
                children = list(listing)
        except OSError as exc:
            raise SealedTreeError(f"cannot list {directory}: {exc.strerror}") from exc
        for child in children:
            info = child.stat(follow_symlinks=False)
            key = prefix + child.name
            entries[key] = _entry(Path(child.path), info, content=content)
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(child.path), f"{key}/"))
    return entries


def _entry(path: Path, before: os.stat_result, *, content: bool) -> Entry:
    kind = _kind(before.st_mode, path)
    target = str(path.readlink()) if kind is EntryKind.LINK else None
    digest = _sha256(path) if content and kind is EntryKind.FILE else None
    if digest is not None:
        after = path.lstat()
        if any(getattr(before, key) != getattr(after, key) for key in _STABLE):
            raise SealedTreeError(f"changed while reading: {path}")
    return Entry(
        kind=kind,
        mode=stat.S_IMODE(before.st_mode),
        size=before.st_size,
        inode=before.st_ino,
        mtime_ns=before.st_mtime_ns,
        ctime_ns=before.st_ctime_ns,
        target=target,
        sha256=digest,
    )


def _kind(mode: int, path: Path) -> EntryKind:
    if stat.S_ISREG(mode):
        return EntryKind.FILE
    if stat.S_ISDIR(mode):
        return EntryKind.DIR
    if stat.S_ISLNK(mode):
        return EntryKind.LINK
    raise SealedTreeError(f"a sealed tree holds only files, directories and symlinks: {path}")


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


# meiva:note
# Remove every write bit, then on macOS set the user-immutable flag, which also refuses rename,
# unlink and new entries in a directory. The order matters: an immutable path refuses `chmod`.
# Neither command follows a symlink met while recursing.
def seal(path: Path, *, host: Host) -> None:
    _run(host, ["chmod", "-R", "a-w", str(path)])
    match host.platform.system:
        case "darwin":
            _run(host, ["chflags", "-R", "uchg", str(path)])
        case "linux":
            pass
        case _:
            assert_never(host.platform.system)


# meiva:note
# The reverse of `seal`, for removing a release: clear the flag first, since it refuses `chmod`.
def unseal(path: Path, *, host: Host) -> None:
    match host.platform.system:
        case "darwin":
            _run(host, ["chflags", "-R", "nouchg", str(path)])
        case "linux":
            pass
        case _:
            assert_never(host.platform.system)
    _run(host, ["chmod", "-R", "u+w", str(path)])


def _run(host: Host, argv: list[str]) -> None:
    done = host.run(argv, capture_output=True, text=True, timeout=_SEAL_TIMEOUT_S)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "no output").strip().splitlines()[-1:]
        raise SealedTreeError(f"`{' '.join(argv)}` exited {done.returncode}: {detail}")


# meiva:note
# The manifest of a tree that is already sealed, so its stat facts are the sealed ones. The tree
# is hashed in full here, once per release.
def snapshot(release: Path) -> Manifest:
    return Manifest(root=str(release), entries=observe(release, content=True))


# meiva:note
# Publish the manifest beside the release, read-only and on macOS immutable like the tree.
def write_manifest(release: Path, manifest: Manifest, *, host: Host) -> Path:
    path = manifest_path(release)
    write_text_atomic(path, manifest.to_json(), encoding="utf-8", mode=0o444, sync_parent=True)
    seal(path, host=host)
    return path


def read_manifest(release: Path) -> Manifest:
    path = manifest_path(release)
    return Manifest.parse(path.read_text(encoding="utf-8"), origin=str(path))


# meiva:note
# The differences between a release and its manifest, empty when it is the tree that was sealed.
# `content=False` compares `lstat` only (about 4 s for an 82k-entry release); `content=True` also
# rehashes every file. The release is resolved first, so a caller may name it through the pointer.
def verify(release: Path, *, content: bool) -> tuple[str, ...]:
    root = release.resolve(strict=True)
    sealed = read_manifest(root)
    return differences(sealed, str(root), observe(root, content=content), content=content)


# meiva:note
# Delete a release and its manifest, sealed or half-built. The caller decides that nothing runs
# from it. The manifest goes first because it is written last: a removal that stops part-way
# leaves a release without one, which the next stage rebuilds, never a manifest that claims a
# finished release that is gone.
def remove(release: Path, *, host: Host) -> None:
    manifest = manifest_path(release)
    if manifest.exists():
        unseal(manifest, host=host)
        manifest.unlink()
    if release.exists():
        unseal(release, host=host)
        shutil.rmtree(release)
