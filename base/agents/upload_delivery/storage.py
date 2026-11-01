# meiva:link base/agents/upload_delivery/docs/storage.meiva.okf.md

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from psycopg import Connection

from base.agents.upload_delivery.models import (
    Manifest,
    Object,
    UploadDeliveryConflictError,
    UploadQuotaExceededError,
)
from base.agents.upload_delivery.paths import STAGED_NAMESPACE
from base.host.private_storage import create_private_bytes
from base.paths.private_dir import ensure_private_dir

LEGACY_NAMESPACE = ".delivered-v1"
_TEMPORARY = re.compile(r"^\..+\.[A-Za-z0-9_]{8}\.tmp$")
_INVENTORY_DEPTH = 3


def lock_agent(conn: Connection[Any], agent_id: int) -> None:
    # Conservatively serialize across machines; physical reservation accounting
    # still binds the actual storage directory, not the independent MEIVA_HOME.
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"upload:{agent_id}",))


# meiva:note
# The files an unfinished reservation owns, as paths relative to the directory it reserves,
# read from its stored manifest without requiring it to be the current protocol: a row an
# earlier protocol left unfinished keeps reserving what it reserved. A protocol-2 manifest
# names `<send directory>/<file>`; a protocol-1 one `.delivered-v1/<batch>/<file>`.
def reserved_paths(manifest: Mapping[str, Any]) -> dict[str, int]:
    directory = manifest.get("directory")
    prefix = (
        str(directory) if directory is not None else f"{LEGACY_NAMESPACE}/{manifest['batch_id']}"
    )
    return {f"{prefix}/{item['name']}": int(item["size"]) for item in manifest["objects"]}


def check_quota(
    conn: Connection[Any],
    agent_id: int,
    directory: Path,
    machine: str,
    incoming_bytes: int,
    incoming_files: int,
    max_bytes: int,
    max_files: int,
    *,
    incoming_manifest: Manifest | None = None,
) -> None:
    owned = _reservations(conn, agent_id, directory, machine)
    total_bytes, total_files = _inventory(directory, owned)
    if incoming_manifest is not None:
        existing = _owned_incoming(directory, incoming_manifest, owned)
        incoming_bytes -= sum(existing)
        incoming_files -= len(existing)
    if total_bytes + incoming_bytes > max_bytes or total_files + incoming_files > max_files:
        raise UploadQuotaExceededError(f"upload exceeds agent {agent_id}'s native physical quota")


def _reservations(
    conn: Connection[Any], agent_id: int, directory: Path, machine: str
) -> dict[str, int]:
    owned: dict[str, int] = {}
    rows = conn.execute(
        "SELECT manifest FROM agent_upload_batches WHERE agent_id=%s AND receipt IS NULL",
        (agent_id,),
    ).fetchall()
    for row in rows:
        for item in row[0]:
            owned[item["stored_name"]] = item["size"]
    resolved = str(directory.resolve())
    rows = conn.execute(
        "SELECT manifest FROM upload_delivery_batches WHERE agent_id=%s AND storage_machine=%s "
        "AND storage_directory=%s AND ready_at IS NULL UNION ALL "
        "SELECT manifest FROM upload_delivery_copies WHERE agent_id=%s AND storage_machine=%s "
        "AND storage_directory=%s AND ready_at IS NULL",
        (agent_id, machine, resolved, agent_id, machine, resolved),
    ).fetchall()
    for row in rows:
        owned.update(reserved_paths(row[0]))
    rows = conn.execute(
        "SELECT file_id, size FROM upload_staged_files WHERE agent_id=%s AND storage_machine=%s "
        "AND storage_directory=%s AND state='receiving'",
        (agent_id, machine, resolved),
    ).fetchall()
    for file_id, size in rows:
        owned[f"{STAGED_NAMESPACE}/{file_id}"] = size
    return owned


def _files(directory: Path, depth: int = 1) -> Iterator[Path]:
    if not directory.is_dir() or depth > _INVENTORY_DEPTH:
        return
    for entry in directory.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _files(entry, depth + 1)
        elif entry.is_file() and not _TEMPORARY.fullmatch(entry.name):
            yield entry


def _inventory(directory: Path, owned: dict[str, int]) -> tuple[int, int]:
    total_bytes, total_files = sum(owned.values()), len(owned)
    for path in _files(directory):
        if path.relative_to(directory).as_posix() not in owned:
            total_bytes += path.stat().st_size
            total_files += 1
    return total_bytes, total_files


def _owned_incoming(directory: Path, manifest: Manifest, owned: dict[str, int]) -> list[int]:
    existing: list[int] = []
    for item in manifest.objects:
        relative = f"{manifest.directory}/{item.name}"
        path = directory / relative
        if path.exists() or path.is_symlink():
            verify(path, item)
            existing.append(item.size)
        elif relative in owned:
            existing.append(item.size)
    return existing


def object_path(directory: Path, manifest: Manifest, item: Object) -> Path:
    path = directory / manifest.directory / item.name
    if path.resolve().parent != (directory.resolve() / manifest.directory):
        raise UploadDeliveryConflictError("immutable upload directory escapes its native root")
    return path


def verify(path: Path, item: Object) -> None:
    if path.is_symlink() or not path.is_file():
        raise UploadDeliveryConflictError("immutable upload object is not a regular file")
    contents = path.read_bytes()
    if len(contents) != item.size or hashlib.sha256(contents).hexdigest() != item.sha256:
        raise UploadDeliveryConflictError("immutable upload object differs from its fixed manifest")


def publish(directory: Path, manifest: Manifest, contents: list[bytes]) -> None:
    """Create only; each concurrent attempt cleans only its own primitive temp."""
    for item, data in zip(manifest.objects, contents, strict=True):
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise UploadDeliveryConflictError("received bytes differ from the fixed manifest")
        path = object_path(directory, manifest, item)
        ensure_private_dir(path.parent)
        try:
            create_private_bytes(path, data)
        except FileExistsError:
            verify(path, item)
    fsync_tree(directory, manifest)


def fsync_tree(directory: Path, manifest: Manifest) -> None:
    for path in (
        directory / manifest.directory,
        directory,
        directory.parent,
        directory.parent.parent,
    ):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def verify_all(directory: Path, manifest: Manifest) -> None:
    for item in manifest.objects:
        verify(object_path(directory, manifest, item), item)
    fsync_tree(directory, manifest)
