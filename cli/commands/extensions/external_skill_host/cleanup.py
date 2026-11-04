"""Crash-recoverable preservation of external skill transaction residue."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal

from cli.commands.extensions.external_skill_host.filesystem import (
    ClientConflictError,
    exists,
    tree_manifest,
    validate_manifest_subset,
    verify_cleanup_file,
)
from cli.commands.extensions.external_skill_host.ledger import (
    SKILL_NAME,
    write_ledger,
    write_retained_item,
)
from cli.commands.extensions.external_skill_host.records import (
    Claim,
    CleanupItem,
    FileEntry,
    Ledger,
    ManifestEntry,
)


def transaction_path(
    skills_root: Path, kind: str, generation_id: str, *, skill_name: str = SKILL_NAME
) -> Path:
    return skills_root / f".{skill_name}.meiva-{kind}-{generation_id}"


def _prepared_path(ledger_path: Path, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-stage-{generation_id}"


def quarantine_path(ledger_path: Path, kind: str, generation_id: str) -> Path:
    return ledger_path.parent / f".{ledger_path.stem}-retained-{kind}-{generation_id}"


def _file_claims(manifest: list[ManifestEntry]) -> list[Claim]:
    claims: list[Claim] = []
    for item in manifest:
        if item["kind"] != "file":
            continue
        relative = item["path"]
        path = Path(relative)
        suffix = hashlib.sha256(relative.encode()).hexdigest()[:16]
        quarantine = path.with_name(f".{path.name}.meiva-retained-{suffix}").as_posix()
        claims.append(Claim(path=relative, quarantine=quarantine, state="source"))
    return claims


def queue_garbage(
    ledger: Ledger,
    *,
    kind: Literal["prepared", "stage", "previous"],
    path_generation_id: str,
    manifest: list[ManifestEntry],
) -> None:
    record = CleanupItem(
        file_claims=_file_claims(manifest),
        kind=kind,
        location="source",
        manifest=manifest,
        path_generation_id=path_generation_id,
    )
    if record not in ledger["garbage"]:
        ledger["garbage"].append(record)


def _source_path(
    ledger_path: Path,
    skills_root: Path,
    kind: str,
    generation_id: str,
    *,
    skill_name: str = SKILL_NAME,
) -> Path:
    if kind == "prepared":
        return _prepared_path(ledger_path, generation_id)
    return transaction_path(skills_root, kind, generation_id, skill_name=skill_name)


def _verify_cleanup_candidate(path: Path, manifest: list[ManifestEntry]) -> None:
    current = tree_manifest(path)
    validate_manifest_subset(current, manifest)


def _restore_cleanup_candidate(
    ledger_path: Path,
    ledger: Ledger,
    item: CleanupItem,
    source: Path,
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> None:
    if exists(quarantine) and not exists(source):
        rename(quarantine, source)
        item["location"] = "source"
        write_ledger(ledger_path, ledger)


def _manifest_files(item: CleanupItem) -> dict[str, FileEntry]:
    return {entry["path"]: entry for entry in item["manifest"] if entry["kind"] == "file"}


def _flush_inline_retained(ledger_path: Path, ledger: Ledger) -> None:
    while ledger["retained"]:
        write_retained_item(ledger_path, ledger["retained"][0])
        ledger["retained"].pop(0)


def _prepare_claim(
    claim: Claim, root: Path, expected: FileEntry
) -> tuple[Claim, Path, Path] | None:
    source = root / claim["path"]
    dest = root / claim["quarantine"]
    state = claim["state"]
    if state == "source":
        if not exists(source) or exists(dest):
            claim["state"] = "retained"
            return None
        try:
            verify_cleanup_file(source, expected)
        except (OSError, ClientConflictError):
            claim["state"] = "retained"
            return None
        claim["state"] = "claiming"
        return (claim, source, dest)
    if state == "claiming":
        if exists(source) and not exists(dest):
            try:
                verify_cleanup_file(source, expected)
            except (OSError, ClientConflictError):
                claim["state"] = "retained"
                return None
            return (claim, source, dest)
        # meiva:note
        # A crash left the rename outcome ambiguous. The write-ahead record names both
        # paths, but path shape or matching bytes cannot establish that the destination is
        # the object Meiva moved. Preserve both.
        claim["state"] = "retained"
        return None
    return None


# meiva:note
# Isolate every file in a quarantined tree using one batched write-ahead ledger commit
# before the renames and one terminal commit after, then archive the retired record out
# of the hot ledger.
def preserve_claimed_tree(
    ledger_path: Path,
    ledger: Ledger,
    item: CleanupItem,
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> None:
    files = _manifest_files(item)
    to_rename: list[tuple[Claim, Path, Path]] = []
    for claim in item["file_claims"]:
        prepared = _prepare_claim(claim, quarantine, files[claim["path"]])
        if prepared is not None:
            to_rename.append(prepared)
    if to_rename:
        write_ledger(ledger_path, ledger)
    for claim, source, dest in to_rename:
        try:
            rename(source, dest)
        except OSError:
            # meiva:note
            # The root is already private. Whether the rename failed or completed before
            # reporting failure, terminal preservation is safe and avoids inferring
            # ownership or retrying a mutable path.
            claim["state"] = "retained"
            continue
        claim["state"] = "quarantine"
    for claim in item["file_claims"]:
        if claim["state"] == "quarantine":
            # meiva:note
            # The root is already isolated under Meiva's private ledger. A changed
            # pathname is never retried or adopted for deletion.
            with suppress(OSError, ClientConflictError):
                verify_cleanup_file(quarantine / claim["quarantine"], files[claim["path"]])
            claim["state"] = "retained"
    item["location"] = "retained"
    write_retained_item(ledger_path, item)
    ledger["garbage"].remove(item)
    _flush_inline_retained(ledger_path, ledger)
    write_ledger(ledger_path, ledger)


def _claim_from_source(
    ledger_path: Path,
    ledger: Ledger,
    item: CleanupItem,
    source: Path,
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> bool:
    """Move the garbage source into quarantine; True when there was nothing left to claim."""
    source_exists = exists(source)
    quarantine_exists = exists(quarantine)
    if not source_exists:
        if quarantine_exists:
            raise ClientConflictError("unclaimed cleanup quarantine exists")
        return True
    if quarantine_exists:
        raise ClientConflictError("cleanup source and quarantine both exist")
    item["location"] = "claiming"
    write_ledger(ledger_path, ledger)
    try:
        rename(source, quarantine)
    except OSError:
        if exists(source) and not exists(quarantine):
            item["location"] = "source"
            write_ledger(ledger_path, ledger)
        raise
    return False


# meiva:note
# Resolve an in-flight claim; False when the source is back and cleanup must be retried.
# Once verified, `location = "quarantine"` rides the immediate `preserve_claimed_tree`
# write-ahead commit rather than taking a separate fsync of its own.
def _settle_claim(
    ledger_path: Path,
    ledger: Ledger,
    item: CleanupItem,
    source: Path,
    quarantine: Path,
    rename: Callable[[Path, Path], None],
) -> bool:
    source_exists = exists(source)
    quarantine_exists = exists(quarantine)
    if source_exists and quarantine_exists:
        raise ClientConflictError("cleanup claim outcome is ambiguous")
    if source_exists:
        item["location"] = "source"
        write_ledger(ledger_path, ledger)
        return False
    if not quarantine_exists:
        raise ClientConflictError("cleanup claim outcome is ambiguous")
    try:
        _verify_cleanup_candidate(quarantine, item["manifest"])
    except (OSError, ClientConflictError):
        _restore_cleanup_candidate(ledger_path, ledger, item, source, quarantine, rename)
        raise
    item["location"] = "quarantine"
    return True


def _cleanup_garbage_item(
    ledger_path: Path,
    ledger: Ledger,
    skills_root: Path,
    item: CleanupItem,
    rename: Callable[[Path, Path], None],
    *,
    skill_name: str = SKILL_NAME,
) -> bool:
    generation_id = item["path_generation_id"]
    source = _source_path(
        ledger_path, skills_root, item["kind"], generation_id, skill_name=skill_name
    )
    quarantine = quarantine_path(ledger_path, item["kind"], generation_id)
    if item["location"] == "source" and _claim_from_source(
        ledger_path, ledger, item, source, quarantine, rename
    ):
        return True
    if item["location"] == "claiming" and not _settle_claim(
        ledger_path, ledger, item, source, quarantine, rename
    ):
        return False
    if not exists(quarantine):
        return True
    preserve_claimed_tree(ledger_path, ledger, item, quarantine, rename)
    return True


def cleanup_garbage_impl(
    ledger_path: Path,
    ledger: Ledger,
    skills_root: Path,
    label: str,
    rename: Callable[[Path, Path], None],
    *,
    skill_name: str = SKILL_NAME,
) -> None:
    for item in list(ledger["garbage"]):
        try:
            if (
                _cleanup_garbage_item(
                    ledger_path, ledger, skills_root, item, rename, skill_name=skill_name
                )
                and item in ledger["garbage"]
            ):
                ledger["garbage"].remove(item)
                _flush_inline_retained(ledger_path, ledger)
                write_ledger(ledger_path, ledger)
        except (OSError, ClientConflictError) as exc:
            print(
                f"  ! {label} external operator skill skipped: conflict: "
                f"transaction cleanup conflict ({type(exc).__name__})",
                file=sys.stderr,
            )
    if ledger["retained"] and ledger["transaction"] is None:
        try:
            _flush_inline_retained(ledger_path, ledger)
            write_ledger(ledger_path, ledger)
        except (OSError, ClientConflictError) as exc:
            print(
                f"  ! {label} external operator skill skipped: conflict: "
                f"transaction cleanup conflict ({type(exc).__name__})",
                file=sys.stderr,
            )
