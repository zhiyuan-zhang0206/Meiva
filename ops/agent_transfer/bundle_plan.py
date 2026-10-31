# meiva:note
# Pure framing, path validation, quota checks, and post-extraction snapshot verification for
# whole-agent workspace transfers.
#
# No filesystem, network, clock, or database access lives here (`purity = "strict"`). Every
# path check and byte-for-byte manifest comparison fails closed on unknown, extra, missing, or
# altered entries.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import PurePosixPath

from pydantic import ValidationError

from ops.rpc_schemas.transfer import WorkspaceBundleManifest, WorkspaceManifestEntry

BUNDLE_MAGIC = b"MEIVA_WS_V1\n"
MAX_WORKSPACE_TRANSFER_BYTES = 256 * 1024 * 1024
MAX_WORKSPACE_TRANSFER_FILES = 20_000
MAX_BUNDLE_STREAM_BYTES = MAX_WORKSPACE_TRANSFER_BYTES + 24 * 1024 * 1024
SYMLINK_NORMALIZED_MODE = 0o777


class WorkspaceTransferError(ValueError):
    reason: str = "transfer_error"


class WorkspaceTransferConflictError(WorkspaceTransferError):
    reason: str = "transfer_conflict"


class WorkspaceTransferTooLargeError(WorkspaceTransferError):
    reason: str = "transfer_too_large"


class WorkspaceTransferCorruptError(WorkspaceTransferError):
    reason: str = "transfer_corrupt"


# meiva:note
# Validate a workspace-relative POSIX path. Rejects absolute paths, Windows separators, NUL bytes,
# `.` or `..` segments, and non-canonical spellings so neither tar packing nor extraction can name
# a location outside the staging directory.
def validate_relpath(relpath: str) -> str:
    if (
        not relpath
        or "\x00" in relpath
        or "\\" in relpath
        or relpath.startswith("/")
        or relpath.endswith("/")
    ):
        raise WorkspaceTransferCorruptError(f"invalid workspace relative path: {relpath!r}")
    pure = PurePosixPath(relpath)
    if pure.is_absolute() or str(pure) != relpath:
        raise WorkspaceTransferCorruptError(f"non-canonical workspace relative path: {relpath!r}")
    for part in pure.parts:
        if part in ("", ".", ".."):
            raise WorkspaceTransferCorruptError(
                f"disallowed segment in workspace relative path: {relpath!r}"
            )
    return relpath


# meiva:note
# Validate one manifest entry's internal invariants (`file` carries `sha256`, `symlink` carries a
# non-NUL `link_target`, `dir` carries neither) and return the entry.
def validate_manifest_entry(entry: WorkspaceManifestEntry) -> WorkspaceManifestEntry:
    validate_relpath(entry.relpath)
    if entry.kind == "file":
        if entry.sha256 is None or len(entry.sha256) != 64 or entry.link_target is not None:
            raise WorkspaceTransferCorruptError(
                f"invalid file manifest entry for {entry.relpath!r}"
            )
    elif entry.kind == "symlink":
        if (
            not entry.link_target
            or "\x00" in entry.link_target
            or entry.sha256 is not None
            or entry.size != 0
        ):
            raise WorkspaceTransferCorruptError(
                f"invalid symlink manifest entry for {entry.relpath!r}"
            )
    elif entry.kind == "dir" and (
        entry.sha256 is not None or entry.link_target is not None or entry.size != 0
    ):
        raise WorkspaceTransferCorruptError(
            f"invalid directory manifest entry for {entry.relpath!r}"
        )
    return entry


# meiva:note
# Enforce entry count and total regular-file byte ceilings before streaming or extracting. Returns
# `(files_count, total_file_bytes)` for regular files while bounding total entries (including
# directories and symlinks) by `max_files`.
def check_workspace_limits(
    entries: Sequence[WorkspaceManifestEntry],
    *,
    max_files: int = MAX_WORKSPACE_TRANSFER_FILES,
    max_bytes: int = MAX_WORKSPACE_TRANSFER_BYTES,
) -> tuple[int, int]:
    seen: set[str] = set()
    files_count = 0
    total_bytes = 0
    if len(entries) > max_files:
        raise WorkspaceTransferTooLargeError(
            f"workspace has {len(entries)} entries, exceeding the {max_files}-entry transfer limit"
        )
    for entry in entries:
        validate_manifest_entry(entry)
        if entry.relpath in seen:
            raise WorkspaceTransferCorruptError(
                f"duplicate workspace manifest entry: {entry.relpath!r}"
            )
        seen.add(entry.relpath)
        if entry.kind == "file":
            files_count += 1
            total_bytes += entry.size
            if total_bytes > max_bytes:
                raise WorkspaceTransferTooLargeError(
                    f"workspace regular files exceed the {max_bytes}-byte transfer limit"
                )
    return files_count, total_bytes


# meiva:note
# Frame `(manifest, tar_bytes)` into a self-verifying `MEIVA_WS_V1` byte stream:
# `BUNDLE_MAGIC + uint32_be(len(manifest_json)) + manifest_json + tar_bytes`.
def encode_bundle(manifest: WorkspaceBundleManifest, tar_bytes: bytes) -> bytes:
    files_count, total_bytes = check_workspace_limits(manifest.entries)
    if manifest.files_count != files_count or manifest.total_file_bytes != total_bytes:
        raise WorkspaceTransferCorruptError("manifest file counts or totals do not match entries")
    actual_sha = hashlib.sha256(tar_bytes).hexdigest()
    if len(tar_bytes) != manifest.tar_bytes or actual_sha != manifest.tar_sha256:
        raise WorkspaceTransferCorruptError("manifest tar digest or length does not match payload")
    manifest_bytes = manifest.model_dump_json().encode("utf-8")
    stream = BUNDLE_MAGIC + len(manifest_bytes).to_bytes(4, "big") + manifest_bytes + tar_bytes
    if len(stream) > MAX_BUNDLE_STREAM_BYTES:
        raise WorkspaceTransferTooLargeError(
            f"encoded workspace bundle ({len(stream)} bytes) exceeds stream limit"
        )
    return stream


# meiva:note
# Parse and verify a framed `MEIVA_WS_V1` bundle stream against the expected `(agent_id, transfer_id)`
# before any file is written to disk.
def decode_bundle(
    bundle_bytes: bytes,
    *,
    expected_agent_id: int,
    expected_transfer_id: str,
) -> tuple[WorkspaceBundleManifest, bytes]:
    if len(bundle_bytes) > MAX_BUNDLE_STREAM_BYTES:
        raise WorkspaceTransferTooLargeError(
            f"workspace bundle ({len(bundle_bytes)} bytes) exceeds stream limit"
        )
    header_len = len(BUNDLE_MAGIC) + 4
    if len(bundle_bytes) < header_len or not bundle_bytes.startswith(BUNDLE_MAGIC):
        raise WorkspaceTransferCorruptError("workspace bundle is missing the MEIVA_WS_V1 header")
    manifest_len = int.from_bytes(bundle_bytes[len(BUNDLE_MAGIC) : header_len], "big")
    manifest_end = header_len + manifest_len
    if manifest_len <= 0 or manifest_end > len(bundle_bytes):
        raise WorkspaceTransferCorruptError("workspace bundle manifest length is out of bounds")
    try:
        manifest = WorkspaceBundleManifest.model_validate_json(
            bundle_bytes[header_len:manifest_end]
        )
    except ValidationError as exc:
        raise WorkspaceTransferCorruptError(f"invalid workspace bundle manifest: {exc}") from exc
    if manifest.agent_id != expected_agent_id or manifest.transfer_id != expected_transfer_id:
        raise WorkspaceTransferCorruptError(
            "workspace bundle identity does not match the requested agent or transfer"
        )
    files_count, total_bytes = check_workspace_limits(manifest.entries)
    if manifest.files_count != files_count or manifest.total_file_bytes != total_bytes:
        raise WorkspaceTransferCorruptError("workspace bundle summary counters are inconsistent")
    tar_bytes = bundle_bytes[manifest_end:]
    if len(tar_bytes) != manifest.tar_bytes:
        raise WorkspaceTransferCorruptError(
            f"workspace tar length mismatch: expected {manifest.tar_bytes}, got {len(tar_bytes)}"
        )
    actual_sha = hashlib.sha256(tar_bytes).hexdigest()
    if actual_sha != manifest.tar_sha256:
        raise WorkspaceTransferCorruptError("workspace tar sha256 mismatch")
    return manifest, tar_bytes


def _normalized_entry(entry: WorkspaceManifestEntry) -> WorkspaceManifestEntry:
    mode = SYMLINK_NORMALIZED_MODE if entry.kind == "symlink" else (entry.mode & 0o777)
    return WorkspaceManifestEntry(
        relpath=entry.relpath,
        kind=entry.kind,
        mode=mode,
        size=entry.size,
        sha256=entry.sha256,
        link_target=entry.link_target,
    )


# meiva:note
# Compare the source manifest entries against a fresh post-extraction disk scan on the target host.
# Fails closed if any entry is missing, unexpected, or differs in type, permission bits, byte size,
# content sha256, or symlink target.
def verify_disk_snapshot(
    expected: Sequence[WorkspaceManifestEntry],
    actual: Sequence[WorkspaceManifestEntry],
) -> None:
    expected_norm = sorted((_normalized_entry(e) for e in expected), key=lambda item: item.relpath)
    actual_norm = sorted((_normalized_entry(a) for a in actual), key=lambda item: item.relpath)
    if len(expected_norm) != len(actual_norm):
        expected_paths = {item.relpath for item in expected_norm}
        actual_paths = {item.relpath for item in actual_norm}
        missing = sorted(expected_paths - actual_paths)[:5]
        extra = sorted(actual_paths - expected_paths)[:5]
        raise WorkspaceTransferCorruptError(
            f"extracted workspace entry count mismatch "
            f"(expected {len(expected_norm)}, got {len(actual_norm)}; "
            f"missing={missing}, extra={extra})"
        )
    for exp, act in zip(expected_norm, actual_norm, strict=True):
        if exp != act:
            raise WorkspaceTransferCorruptError(
                f"extracted workspace mismatch at {exp.relpath!r}: expected {exp}, got {act}"
            )
