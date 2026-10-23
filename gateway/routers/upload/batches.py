"""Silent upload identities, receiving reservations and immutable file publication.

Receiving identities never expire: recovery supplies the original bytes. PostgreSQL
transaction locks serialize quota admission; create-only files also fence writers
that continue after their database connection has died.
"""

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.upload_delivery.paths import upload_url
from base.api_contracts.agent_contract import AgentNotFound
from base.db import agent_exists
from base.db.transaction import write_transaction
from base.host.private_storage import create_private_bytes
from base.paths.private_dir import ensure_private_dir
from gateway.routers.upload.upload_plan import (
    SuffixRefusal,
    UploadItem,
    batch_fingerprint,
    plan_manifest,
)
from gateway.schemas.uploads import UploadedBatch, UploadedFile

__all__ = ["Claim", "UploadItem", "claim_batch", "publish_files", "save_keyed_batch"]


def lock_agent(conn: Connection[Any], agent_id: int) -> None:
    """Take the transaction-scoped quota gate shared with every other upload writer."""
    from base.agents.upload_delivery.storage import lock_agent as native_lock_agent

    native_lock_agent(conn, agent_id)


def check_quota(
    conn: Connection[Any],
    agent_id: int,
    directory: Path,
    incoming_bytes: int,
    incoming_files: int,
    max_bytes: int,
    max_files: int,
    *,
    local_machine: str,
) -> None:
    """Count receiving objects once, including their not-yet-published bytes."""
    from base.agents.upload_delivery.models import UploadQuotaExceededError
    from base.agents.upload_delivery.storage import check_quota as native_check_quota

    try:
        native_check_quota(
            conn,
            agent_id,
            directory,
            local_machine,
            incoming_bytes,
            incoming_files,
            max_bytes,
            max_files,
        )
    except UploadQuotaExceededError as exc:
        raise HTTPException(413, str(exc)) from exc


# meiva:note
# The committed identity of one upload operation: what it is, its objects, its receipt.
#
# `receipt` is None until the files are published and the receipt committed.
@dataclass(frozen=True)
class Claim:
    fingerprint: str
    manifest: list[dict[str, Any]]
    receipt: dict[str, Any] | None


def _lookup(conn: Connection[Any], key: str, fingerprint: str) -> Claim | None:
    row = conn.execute(
        "SELECT request_fingerprint, manifest, receipt FROM agent_upload_batches WHERE operation_key = %s",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if row[0] != fingerprint:
        raise HTTPException(409, "idempotency key was already used for a different upload manifest")
    return Claim(row[0], row[1], row[2])


# meiva:note
# Commit the receiving identity and reserve its quota, or return the one already there.
def claim_batch(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    directory: Path,
    batch: list[UploadItem],
    names: list[str],
    *,
    max_bytes: int,
    max_files: int,
    local_machine: str,
) -> Claim:
    fingerprint = batch_fingerprint(batch, names)
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        found = _lookup(conn, key, fingerprint)
        if found is not None:
            return found
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
        manifest = plan_manifest(batch, names, uuid.uuid4().hex)
        if isinstance(manifest, SuffixRefusal):
            raise HTTPException(
                422, "upload suffix cannot be represented in an immutable object URL"
            )
        ensure_private_dir(directory)
        check_quota(
            conn,
            agent_id,
            directory,
            sum(len(item[1]) for item in batch),
            len(batch),
            max_bytes,
            max_files,
            local_machine=local_machine,
        )
        conn.execute(
            "INSERT INTO agent_upload_batches (operation_key, agent_id, request_fingerprint, manifest) "
            "VALUES (%s, %s, %s, %s)",
            (key, agent_id, fingerprint, Jsonb(manifest)),
        )
        return Claim(fingerprint, manifest, None)


def _verify(path: Path, item: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise HTTPException(409, "immutable upload object is not a regular file")
    contents = path.read_bytes()
    if len(contents) != item["size"] or hashlib.sha256(contents).hexdigest() != item["sha256"]:
        raise HTTPException(409, "immutable upload object does not match its manifest")


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_files(directory: Path, manifest: list[dict[str, Any]], batch: list[UploadItem]) -> None:
    """Publish without replacing or removing any final name, even on failure."""
    staging = ensure_private_dir(directory / ".batch-staging" / uuid.uuid4().hex)
    try:
        for ordinal, (item, (_, contents, _)) in enumerate(zip(manifest, batch, strict=True)):
            temporary = staging / str(ordinal)
            create_private_bytes(temporary, contents)
            final = directory / item["stored_name"]
            try:
                os.link(temporary, final)
            except FileExistsError:
                _verify(final, item)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)
        _fsync_directory(directory.parent.parent)
    finally:
        # Never sweep another attempt: a writer can outlive its DB connection.
        shutil.rmtree(staging)


def save_keyed_batch(
    pool: ConnectionPool,
    key: str,
    agent_id: int,
    directory: Path,
    batch: list[UploadItem],
    names: list[str],
    *,
    max_bytes: int,
    max_files: int,
    local_machine: str,
) -> UploadedBatch:
    """Claim first, then publish and commit an immutable receipt in a second transaction."""
    claimed = claim_batch(
        pool,
        key,
        agent_id,
        directory,
        batch,
        names,
        max_bytes=max_bytes,
        max_files=max_files,
        local_machine=local_machine,
    )
    if claimed.receipt is not None:
        return UploadedBatch.model_validate(claimed.receipt)
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        found = _lookup(conn, key, claimed.fingerprint)
        assert found is not None  # noqa: S101 -- committed receiving identity is never removed
        if found.receipt is not None:
            return UploadedBatch.model_validate(found.receipt)
        ensure_private_dir(directory)
        publish_files(directory, found.manifest, batch)
        receipt = UploadedBatch(
            files=[
                UploadedFile(
                    filename=item["filename"],
                    path=str(directory / item["stored_name"]),
                    url=upload_url(agent_id, item["stored_name"]),
                    size=item["size"],
                    content_type=item["content_type"],
                )
                for item in found.manifest
            ]
        )
        conn.execute(
            "UPDATE agent_upload_batches SET receipt = %s, ready_at = now() WHERE operation_key = %s",
            (Jsonb(receipt.model_dump()), key),
        )
    return receipt
