# meiva:note
# Deterministic workspace archive packing, safe symlink-aware extraction without `extractall`,
# post-extraction disk verification, and atomic staging/activation swaps under `<home>/workspaces`.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

from base import paths
from ops.agent_transfer.budget import (
    WORKSPACE_TRANSFER_BYTES_SETTING,
    WorkspaceTransferLimits,
    over_budget_during_walk_error,
    over_budget_error,
)
from ops.agent_transfer.bundle_plan import (
    SYMLINK_NORMALIZED_MODE,
    check_workspace_limits,
    decode_bundle,
    encode_bundle,
    validate_relpath,
    verify_disk_snapshot,
)
from ops.agent_transfer.errors import (
    WorkspaceTransferCorruptError,
    WorkspaceTransferTooLargeError,
)
from ops.rpc_schemas.transfer import WorkspaceBundleManifest, WorkspaceManifestEntry

_READ_CHUNK_BYTES = 1024 * 1024


def _staging_dir(workspaces: Path, agent_id: int, transfer_id: str) -> Path:
    return workspaces / f".incoming-{agent_id}-{transfer_id}"


def _backup_dir(workspaces: Path, agent_id: int, transfer_id: str) -> Path:
    return workspaces / f".pre-transfer-{agent_id}-{transfer_id}"


def _retired_dir(workspaces: Path, agent_id: int, transfer_id: str) -> Path:
    return workspaces / f".retired-{agent_id}-{transfer_id}"


# meiva:note
# Read one regular file into memory while hashing it, refusing the moment its bytes pass what the
# byte ceiling leaves for it, so a file over the ceiling is never buffered whole.
def _sha256_and_bytes(
    path: Path,
    *,
    relpath: str,
    max_remaining: int,
    limits: WorkspaceTransferLimits,
) -> tuple[str, bytes]:
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_remaining:
                raise WorkspaceTransferTooLargeError(
                    f"workspace file {relpath!r} is {total} bytes, past the {max_remaining}-byte "
                    f"remainder of the {limits.max_bytes}-byte transfer ceiling "
                    f"({WORKSPACE_TRANSFER_BYTES_SETTING})"
                )
            digest.update(chunk)
            chunks.append(chunk)
    return digest.hexdigest(), b"".join(chunks)


# meiva:note
# The workspace a transfer may carry: `root` when it is a real directory, None when the agent never
# created one. A symlinked root or a non-directory fails closed.
def _workspace_root(root: Path) -> Path | None:
    if root.is_symlink():
        raise WorkspaceTransferCorruptError("workspace root itself must not be a symlink")
    if not root.exists():
        return None
    if not root.is_dir():
        raise WorkspaceTransferCorruptError("workspace root is not a directory")
    return root


# meiva:note
# Count a workspace's entries (files, directories and symlinks) and its original file bytes from
# `lstat` alone, reading no file content, and refuse it when either axis is over `limits`. This is
# the budget the source checks before it kills the agent's persistent shells or streams a bundle: a
# workspace an order of magnitude over the entry ceiling is refused after one walk instead of after
# hashing and buffering every byte it holds.
def preflight_workspace_budget(root: Path, *, limits: WorkspaceTransferLimits) -> None:
    if _workspace_root(root) is None:
        return
    entries = 0
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        entries += len(dirnames) + len(filenames)
        for name in filenames:
            st = os.lstat(current / name)
            if stat.S_ISREG(st.st_mode):
                total_bytes += st.st_size
    if entries > limits.max_files or total_bytes > limits.max_bytes:
        raise over_budget_error(limits, entries=entries, total_bytes=total_bytes)


# meiva:note
# Walk `root` without following symlinks and return canonical `WorkspaceManifestEntry` items sorted
# by `relpath`. Refuses FIFOs, sockets, and device nodes fail-closed so an unrepresentable entry is
# never silently skipped. The two inline ceilings stop the walk early — entries before any content
# is read, bytes before an oversized file is buffered — and the manifest check at the end reports
# the complete totals.
def scan_workspace_tree(
    root: Path,
    *,
    limits: WorkspaceTransferLimits,
) -> list[WorkspaceManifestEntry]:
    if _workspace_root(root) is None:
        return []

    relpaths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            rel = (current / name).relative_to(root).as_posix()
            validate_relpath(rel)
            relpaths.append(rel)
            if len(relpaths) > limits.max_files:
                raise over_budget_during_walk_error(limits, entries=len(relpaths), bytes_read=0)

    relpaths.sort()
    entries: list[WorkspaceManifestEntry] = []
    total_bytes = 0
    for rel in relpaths:
        target = root / PurePosixPath(rel)
        st = os.lstat(target)
        file_mode = st.st_mode
        if stat.S_ISLNK(file_mode):
            link_target = str(target.readlink())
            entries.append(
                WorkspaceManifestEntry(
                    relpath=rel,
                    kind="symlink",
                    mode=SYMLINK_NORMALIZED_MODE,
                    size=0,
                    link_target=link_target,
                )
            )
        elif stat.S_ISDIR(file_mode):
            entries.append(
                WorkspaceManifestEntry(
                    relpath=rel,
                    kind="dir",
                    mode=stat.S_IMODE(file_mode) & 0o777,
                    size=0,
                )
            )
        elif stat.S_ISREG(file_mode):
            digest = hashlib.sha256()
            size = 0
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                while True:
                    chunk = handle.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if total_bytes + size > limits.max_bytes:
                        raise over_budget_during_walk_error(
                            limits,
                            entries=len(relpaths),
                            bytes_read=total_bytes + size,
                        )
                    digest.update(chunk)
            total_bytes += size
            entries.append(
                WorkspaceManifestEntry(
                    relpath=rel,
                    kind="file",
                    mode=stat.S_IMODE(file_mode) & 0o777,
                    size=size,
                    sha256=digest.hexdigest(),
                )
            )
        else:
            raise WorkspaceTransferCorruptError(
                f"unsupported special file in workspace: {rel!r} (mode={oct(file_mode)})"
            )
    check_workspace_limits(entries, limits=limits)
    return entries


def _empty_workspace_bundle(
    *,
    agent_id: int,
    transfer_id: str,
    source_machine: str,
    killed_shell_sessions: list[int],
    limits: WorkspaceTransferLimits,
) -> tuple[WorkspaceBundleManifest, bytes]:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz", format=tarfile.PAX_FORMAT):
        pass
    tar_bytes = tar_buf.getvalue()
    manifest = WorkspaceBundleManifest(
        agent_id=agent_id,
        transfer_id=transfer_id,
        source_machine=source_machine,
        entries=[],
        files_count=0,
        total_file_bytes=0,
        tar_bytes=len(tar_bytes),
        tar_sha256=hashlib.sha256(tar_bytes).hexdigest(),
        killed_shell_sessions=killed_shell_sessions,
    )
    return manifest, encode_bundle(manifest, tar_bytes, limits=limits)


def _pack_single_entry(
    archive: tarfile.TarFile,
    ws_dir: Path,
    rel: str,
    *,
    max_remaining: int,
    limits: WorkspaceTransferLimits,
) -> tuple[WorkspaceManifestEntry, int]:
    target = ws_dir / PurePosixPath(rel)
    st = os.lstat(target)
    file_mode = st.st_mode
    info = tarfile.TarInfo(name=rel)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = int(st.st_mtime)
    if stat.S_ISLNK(file_mode):
        link_target = str(target.readlink())
        info.type = tarfile.SYMTYPE
        info.mode = SYMLINK_NORMALIZED_MODE
        info.size = 0
        info.linkname = link_target
        archive.addfile(info)
        return (
            WorkspaceManifestEntry(
                relpath=rel,
                kind="symlink",
                mode=SYMLINK_NORMALIZED_MODE,
                size=0,
                link_target=link_target,
            ),
            0,
        )
    if stat.S_ISDIR(file_mode):
        perm = stat.S_IMODE(file_mode) & 0o777
        info.type = tarfile.DIRTYPE
        info.mode = perm
        info.size = 0
        archive.addfile(info)
        return WorkspaceManifestEntry(relpath=rel, kind="dir", mode=perm, size=0), 0
    if stat.S_ISREG(file_mode):
        perm = stat.S_IMODE(file_mode) & 0o777
        sha256, content = _sha256_and_bytes(
            target, relpath=rel, max_remaining=max_remaining, limits=limits
        )
        info.type = tarfile.REGTYPE
        info.mode = perm
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
        return (
            WorkspaceManifestEntry(
                relpath=rel,
                kind="file",
                mode=perm,
                size=len(content),
                sha256=sha256,
            ),
            len(content),
        )
    raise WorkspaceTransferCorruptError(
        f"unsupported special file in workspace: {rel!r} (mode={oct(file_mode)})"
    )


# meiva:note
# Pack `<home>/workspaces/<agent_id>` into a deterministic `MEIVA_WS_V1` bundle stream.
#
# An agent that never created its workspace directory exports cleanly as an empty 0-entry manifest
# and materializes an empty workspace directory on the target machine. If a file changes on disk
# while packing, the re-scan check rejects the export fail-closed.
def pack_workspace(
    *,
    home: Path,
    agent_id: int,
    transfer_id: str,
    source_machine: str,
    killed_shell_sessions: list[int],
    limits: WorkspaceTransferLimits,
) -> tuple[WorkspaceBundleManifest, bytes]:
    ws_dir = paths.workspace_path(home, agent_id)
    if _workspace_root(ws_dir) is None:
        return _empty_workspace_bundle(
            agent_id=agent_id,
            transfer_id=transfer_id,
            source_machine=source_machine,
            killed_shell_sessions=killed_shell_sessions,
            limits=limits,
        )

    relpaths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ws_dir, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current = Path(dirpath)
        for name in (*dirnames, *filenames):
            rel = (current / name).relative_to(ws_dir).as_posix()
            validate_relpath(rel)
            relpaths.append(rel)
            if len(relpaths) > limits.max_files:
                raise over_budget_during_walk_error(limits, entries=len(relpaths), bytes_read=0)
    relpaths.sort()

    entries: list[WorkspaceManifestEntry] = []
    total_bytes = 0
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for rel in relpaths:
            entry, added_bytes = _pack_single_entry(
                archive,
                ws_dir,
                rel,
                max_remaining=limits.max_bytes - total_bytes,
                limits=limits,
            )
            total_bytes += added_bytes
            entries.append(entry)

    after_entries = scan_workspace_tree(ws_dir, limits=limits)
    verify_disk_snapshot(entries, after_entries)

    files_count, checked_bytes = check_workspace_limits(entries, limits=limits)
    tar_bytes = tar_buf.getvalue()
    manifest = WorkspaceBundleManifest(
        agent_id=agent_id,
        transfer_id=transfer_id,
        source_machine=source_machine,
        entries=entries,
        files_count=files_count,
        total_file_bytes=checked_bytes,
        tar_bytes=len(tar_bytes),
        tar_sha256=hashlib.sha256(tar_bytes).hexdigest(),
        killed_shell_sessions=killed_shell_sessions,
    )
    return manifest, encode_bundle(manifest, tar_bytes, limits=limits)


def _ensure_no_symlink_ancestors(stage_dir: Path, relpath: str) -> Path:
    parts = PurePosixPath(relpath).parts
    cursor = stage_dir
    for part in parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise WorkspaceTransferCorruptError(
                f"archive entry {relpath!r} traverses symlink ancestor {cursor.name!r}"
            )
        if not cursor.exists():
            cursor.mkdir(mode=0o755)
        elif not cursor.is_dir():
            raise WorkspaceTransferCorruptError(
                f"archive entry {relpath!r} has non-directory ancestor {cursor.name!r}"
            )
    dest = cursor / parts[-1]
    if dest.exists() or dest.is_symlink():
        raise WorkspaceTransferCorruptError(f"duplicate extracted path: {relpath!r}")
    return dest


def _extract_regular_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: WorkspaceManifestEntry,
    dest: Path,
) -> None:
    if expected.kind != "file":
        raise WorkspaceTransferCorruptError(
            f"tar member {expected.relpath!r} is a regular file, expected {expected.kind}"
        )
    extracted = archive.extractfile(member)
    if extracted is None:
        raise WorkspaceTransferCorruptError(f"tar file {expected.relpath!r} has no byte stream")
    digest = hashlib.sha256()
    written = 0
    with dest.open("wb") as out:
        while True:
            chunk = extracted.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > expected.size:
                raise WorkspaceTransferCorruptError(
                    f"tar file {expected.relpath!r} exceeds manifest size {expected.size}"
                )
            digest.update(chunk)
            out.write(chunk)
    if written != expected.size or digest.hexdigest() != expected.sha256:
        raise WorkspaceTransferCorruptError(
            f"tar file {expected.relpath!r} failed size or sha256 verification"
        )
    dest.chmod(expected.mode & 0o777)


def _extract_one_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: WorkspaceManifestEntry,
    stage_dir: Path,
    pending_dir_modes: list[tuple[Path, int]],
) -> None:
    dest = _ensure_no_symlink_ancestors(stage_dir, expected.relpath)
    if member.isdir():
        if expected.kind != "dir":
            raise WorkspaceTransferCorruptError(
                f"tar member {expected.relpath!r} is a directory, expected {expected.kind}"
            )
        dest.mkdir(mode=0o700)
        pending_dir_modes.append((dest, expected.mode & 0o777))
        return
    if member.issym():
        if expected.kind != "symlink" or member.linkname != expected.link_target:
            raise WorkspaceTransferCorruptError(
                f"tar symlink {expected.relpath!r} does not match manifest"
            )
        dest.symlink_to(member.linkname)
        return
    if member.isreg():
        _extract_regular_member(archive, member, expected, dest)
        return
    raise WorkspaceTransferCorruptError(f"unsupported tar member kind for {expected.relpath!r}")


def _extract_tar_to_stage(
    tar_bytes: bytes,
    manifest: WorkspaceBundleManifest,
    stage_dir: Path,
) -> None:
    expected_by_path = {entry.relpath: entry for entry in manifest.entries}
    pending_dir_modes: list[tuple[Path, int]] = []
    seen_members: set[str] = set()

    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as archive:
        for member in archive:
            rel = validate_relpath(member.name)
            if rel in seen_members:
                raise WorkspaceTransferCorruptError(f"duplicate tar member: {rel!r}")
            seen_members.add(rel)
            expected = expected_by_path.get(rel)
            if expected is None:
                raise WorkspaceTransferCorruptError(
                    f"tar archive contains unexpected member {rel!r}"
                )
            _extract_one_member(archive, member, expected, stage_dir, pending_dir_modes)

    for dir_path, mode in reversed(pending_dir_modes):
        dir_path.chmod(mode)


# meiva:note
# Verify `<home>/workspaces` is writable on the target host before the source exports anything.
def prepare_target_workspace(*, home: Path, agent_id: int, transfer_id: str) -> tuple[Path, bool]:
    workspaces = paths.workspaces_root(home=home)
    workspaces.mkdir(parents=True, exist_ok=True)
    probe = workspaces / f".prepare-{agent_id}-{transfer_id}"
    probe.write_bytes(b"ok")
    probe.unlink()
    ws_dir = paths.workspace_path(home, agent_id)
    return ws_dir, (ws_dir.exists() or ws_dir.is_symlink())


# meiva:note
# Decode `bundle_bytes`, extract into a private staging directory under `<home>/workspaces`, rescan
# the extracted tree on disk and verify every entry against the manifest, then atomically swap into
# `<home>/workspaces/<agent_id>` (preserving any prior target directory at `.pre-transfer-...` so an
# abort can restore it cleanly).
def stage_and_activate_workspace(
    *,
    home: Path,
    agent_id: int,
    transfer_id: str,
    bundle_bytes: bytes,
    limits: WorkspaceTransferLimits,
) -> WorkspaceBundleManifest:
    manifest, tar_bytes = decode_bundle(
        bundle_bytes,
        expected_agent_id=agent_id,
        expected_transfer_id=transfer_id,
        limits=limits,
    )
    workspaces = paths.workspaces_root(home=home)
    workspaces.mkdir(parents=True, exist_ok=True)
    stage_dir = _staging_dir(workspaces, agent_id, transfer_id)
    backup_dir = _backup_dir(workspaces, agent_id, transfer_id)
    final_dir = paths.workspace_path(home, agent_id)

    if stage_dir.exists() or stage_dir.is_symlink():
        shutil.rmtree(stage_dir)
    if backup_dir.exists() or backup_dir.is_symlink():
        shutil.rmtree(backup_dir)

    stage_dir.mkdir(mode=0o755)
    try:
        _extract_tar_to_stage(tar_bytes, manifest, stage_dir)
        actual_entries = scan_workspace_tree(stage_dir, limits=limits)
        verify_disk_snapshot(manifest.entries, actual_entries)

        backed_up = False
        if final_dir.exists() or final_dir.is_symlink():
            final_dir.replace(backup_dir)
            backed_up = True
        try:
            stage_dir.replace(final_dir)
        except BaseException:
            if backed_up and backup_dir.exists() and not final_dir.exists():
                backup_dir.replace(final_dir)
            raise
    except BaseException:
        if stage_dir.exists() or stage_dir.is_symlink():
            shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return manifest


def _remove_path(path: Path) -> bool:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return True
    if path.exists():
        shutil.rmtree(path)
        return True
    return False


# meiva:note
# Post-cutover cleanup on either `source` (retire `<home>/workspaces/<agent_id>`) or `target`
# (remove any `.pre-transfer-<agent_id>-<transfer_id>` backup directory).
def finalize_workspace(*, home: Path, agent_id: int, transfer_id: str, role: str) -> bool:
    workspaces = paths.workspaces_root(home=home)
    if not workspaces.exists():
        return False
    if role == "target":
        stage_removed = _remove_path(_staging_dir(workspaces, agent_id, transfer_id))
        backup_removed = _remove_path(_backup_dir(workspaces, agent_id, transfer_id))
        return stage_removed or backup_removed
    if role == "source":
        final_dir = paths.workspace_path(home, agent_id)
        retired_dir = _retired_dir(workspaces, agent_id, transfer_id)
        _remove_path(retired_dir)
        if final_dir.exists() or final_dir.is_symlink():
            final_dir.replace(retired_dir)
            return _remove_path(retired_dir)
        return False
    raise ValueError(f"unknown finalize role: {role!r}")


# meiva:note
# Roll back a staged/activated target workspace when the gateway's Postgres cutover CAS fails,
# restoring any `.pre-transfer-<agent_id>-<transfer_id>` directory that existed beforehand.
def abort_target_workspace(*, home: Path, agent_id: int, transfer_id: str) -> bool:
    workspaces = paths.workspaces_root(home=home)
    if not workspaces.exists():
        return False
    _remove_path(_staging_dir(workspaces, agent_id, transfer_id))
    final_dir = paths.workspace_path(home, agent_id)
    backup_dir = _backup_dir(workspaces, agent_id, transfer_id)
    _remove_path(final_dir)
    if backup_dir.exists() or backup_dir.is_symlink():
        backup_dir.replace(final_dir)
        return True
    return False
