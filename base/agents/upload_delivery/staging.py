# meiva:note
# Staged files: the bytes a person picked for their next send. A file is staged on the gateway
# the moment it is picked, so the composer can show it with its upload progress. Pressing send
# takes the staged files into one batch (`base.agents.upload_delivery.source.send`); a file
# nobody sends is a draft and is swept after `STAGED_FILE_TTL_SECONDS`. Once a batch is
# accepted the bytes of its staged files are deleted (`release_accepted`); the rows stay as
# the evidence of what was delivered.
# meiva:link gateway/upload_delivery/docs/delivered-uploads/source-publication.meiva.okf.md

import hashlib
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from base.agents.upload_delivery.models import (
    StagedFile,
    UploadDeliveryConflictError,
    current_unit,
    staged_request_hash,
)
from base.agents.upload_delivery.naming import clean_filename
from base.agents.upload_delivery.paths import agent_upload_dir, staged_path
from base.agents.upload_delivery.storage import check_quota, lock_agent
from base.api_contracts.agent_contract import AgentNotFound
from base.db import agent_exists
from base.db.transaction import ConnectionSource, write_transaction
from base.host.db_authority.unit import UnitIdentity
from base.host.private_storage import create_private_bytes

__all__ = [
    "claim_for_batch",
    "discard",
    "release_accepted",
    "stage",
    "staged_object",
    "sweep_unsent",
]


def _staged(row: tuple[Any, ...]) -> StagedFile:
    return StagedFile(file_id=row[0], name=row[2], size=row[3], sha256=row[4], content_type=row[5])


def _verify_bytes(path: Path, size: int, sha256: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise UploadDeliveryConflictError("staged upload is not a regular file")
    contents = path.read_bytes()
    if len(contents) != size or hashlib.sha256(contents).hexdigest() != sha256:
        raise UploadDeliveryConflictError("staged upload differs from its recorded identity")


def _claim(
    pool: ConnectionSource,
    key: str,
    agent_id: int,
    file: StagedFile,
    fingerprint: str,
    directory: Path,
    *,
    unit: UnitIdentity,
    max_bytes: int,
    max_files: int,
) -> tuple[Any, ...]:
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        found = conn.execute(
            "SELECT file_id, request_hash, name, size, sha256, content_type, state, "
            "storage_directory FROM upload_staged_files WHERE operation_key=%s",
            (key,),
        ).fetchone()
        if found is not None:
            if found[1] != fingerprint or found[7] != str(directory):
                raise UploadDeliveryConflictError(
                    "idempotency key already identifies another staged file"
                )
            return found
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
        check_quota(conn, agent_id, directory, unit.machine, file.size, 1, max_bytes, max_files)
        conn.execute(
            "INSERT INTO upload_staged_files (file_id, operation_key, agent_id, request_hash, "
            "name, size, sha256, content_type, source_unit, storage_machine, storage_directory) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                file.file_id,
                key,
                agent_id,
                fingerprint,
                file.name,
                file.size,
                file.sha256,
                file.content_type,
                Jsonb(unit.model_dump()),
                unit.machine,
                str(directory),
            ),
        )
        return (
            file.file_id,
            fingerprint,
            file.name,
            file.size,
            file.sha256,
            file.content_type,
            "receiving",
            str(directory),
        )


# meiva:note
# Stage one picked file: reserve its quota and identity, publish the bytes create-only, then
# mark it ready. `file_id` names a new file and is unused when `key` already identifies one.
# A retry of the same key and bytes finishes whatever the first attempt did not, so a
# crash or a lost response never leaves a half-staged file the person cannot resend.
def stage(
    pool: ConnectionSource,
    key: str,
    agent_id: int,
    *,
    raw_name: str,
    data: bytes,
    content_type: str,
    file_id: str,
    upload_root: Path,
    local_machine: str,
    home: Path,
    max_bytes: int,
    max_files: int,
) -> StagedFile:
    candidate = StagedFile(
        file_id=file_id,
        name=clean_filename(raw_name),
        size=len(data),
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
    )
    fingerprint = staged_request_hash(
        candidate.name, candidate.size, candidate.sha256, candidate.content_type
    )
    directory = agent_upload_dir(agent_id, create=False, root=upload_root).resolve()
    row = _claim(
        pool,
        key,
        agent_id,
        candidate,
        fingerprint,
        directory,
        unit=current_unit(local_machine=local_machine, home=home),
        max_bytes=max_bytes,
        max_files=max_files,
    )
    file = _staged(row)
    if row[6] != "receiving":
        return file
    path = staged_path(directory, file.file_id)
    try:
        create_private_bytes(path, data)
    except FileExistsError:
        _verify_bytes(path, file.size, file.sha256)
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        conn.execute(
            "UPDATE upload_staged_files SET state='ready' WHERE file_id=%s AND state='receiving'",
            (file.file_id,),
        )
    return file


def _own(row_unit: object, unit: UnitIdentity) -> None:
    if UnitIdentity.model_validate(row_unit) != unit:
        raise UploadDeliveryConflictError("staged upload belongs to another native unit")


# meiva:note
# Remove a staged file the person took off the composer. The bytes go before the row, so a
# crash leaves a row without bytes (swept with the other drafts), never bytes without a row.
# A file a live batch has taken cannot be discarded; one a held batch took can, which is how
# a person gives up on a held send.
def discard(
    pool: ConnectionSource, agent_id: int, file_id: str, *, local_machine: str, home: Path
) -> None:
    unit = current_unit(local_machine=local_machine, home=home)
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        row = conn.execute(
            "SELECT s.state, s.batch_id, s.storage_directory, s.source_unit, b.state "
            "FROM upload_staged_files s LEFT JOIN upload_delivery_batches b "
            "ON b.batch_id = s.batch_id WHERE s.file_id=%s AND s.agent_id=%s FOR UPDATE OF s",
            (file_id, agent_id),
        ).fetchone()
        if row is None:
            return
        if row[0] == "released" or (row[1] is not None and row[4] != "hold"):
            raise UploadDeliveryConflictError("a send has taken this file")
        _own(row[3], unit)
        staged_path(Path(row[2]), file_id).unlink(missing_ok=True)
        conn.execute("DELETE FROM upload_staged_files WHERE file_id=%s", (file_id,))


# meiva:note
# Sweep the drafts nobody sent: a staged file no batch has taken, older than `ttl_seconds`
# by the database clock. Rows another gateway staged are left to it. A row a send holds a
# lock on is skipped, so a send in progress never loses its file to the sweep.
def sweep_unsent(
    pool: ConnectionSource, *, local_machine: str, home: Path, ttl_seconds: float, limit: int = 16
) -> int:
    unit = current_unit(local_machine=local_machine, home=home)
    with write_transaction(pool) as conn:
        rows = conn.execute(
            "SELECT file_id, storage_directory FROM upload_staged_files WHERE batch_id IS NULL "
            "AND source_unit=%s AND created_at < now() - make_interval(secs => %s) "
            "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (Jsonb(unit.model_dump()), ttl_seconds, limit),
        ).fetchall()
        for file_id, directory in rows:
            staged_path(Path(directory), file_id).unlink(missing_ok=True)
            conn.execute("DELETE FROM upload_staged_files WHERE file_id=%s", (file_id,))
    return len(rows)


# meiva:note
# Delete the staged bytes of batches that were accepted: both chats are committed and the
# agent's unit holds the verified copy, so nothing reads the staging bytes again. The join
# on `state='accepted'` is the whole safety rule — pending and held batches keep their
# bytes — and each row is marked released only after its file is gone, so a crash between
# the two is repaired by the next call. `batch_id` limits the call to one batch.
def release_accepted(
    pool: ConnectionSource,
    *,
    local_machine: str,
    home: Path,
    batch_id: str | None = None,
    limit: int = 16,
) -> int:
    unit = current_unit(local_machine=local_machine, home=home)
    with write_transaction(pool) as conn:
        rows = conn.execute(
            "SELECT s.file_id, s.storage_directory FROM upload_staged_files s "
            "JOIN upload_delivery_batches b ON b.batch_id = s.batch_id "
            "WHERE s.state='ready' AND b.state='accepted' AND s.source_unit=%s "
            "AND (%s::text IS NULL OR s.batch_id=%s) "
            "ORDER BY s.created_at LIMIT %s FOR UPDATE OF s SKIP LOCKED",
            (Jsonb(unit.model_dump()), batch_id, batch_id, limit),
        ).fetchall()
        for file_id, directory in rows:
            staged_path(Path(directory), file_id).unlink(missing_ok=True)
            conn.execute(
                "UPDATE upload_staged_files SET state='released', released_at=now() "
                "WHERE file_id=%s",
                (file_id,),
            )
    return len(rows)


def claim_for_batch(
    conn: Connection[Any],
    agent_id: int,
    file_ids: list[str],
    *,
    unit: UnitIdentity,
    directory: Path,
) -> list[StagedFile]:
    if not file_ids or len(set(file_ids)) != len(file_ids):
        raise UploadDeliveryConflictError("upload send requires one or more distinct staged files")
    rows = conn.execute(
        "SELECT s.file_id, s.request_hash, s.name, s.size, s.sha256, s.content_type, s.state, "
        "s.storage_directory, s.source_unit, s.batch_id, b.state "
        "FROM upload_staged_files s LEFT JOIN upload_delivery_batches b "
        "ON b.batch_id = s.batch_id WHERE s.agent_id=%s AND s.file_id = ANY(%s) FOR UPDATE OF s",
        (agent_id, file_ids),
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    if len(by_id) != len(file_ids):
        raise UploadDeliveryConflictError("one or more staged files do not exist for this agent")
    files: list[StagedFile] = []
    for fid in file_ids:
        row = by_id[fid]
        if row[6] != "ready" or (row[9] is not None and row[10] != "hold"):
            raise UploadDeliveryConflictError("staged file is not ready to be sent")
        _own(row[8], unit)
        if row[7] != str(directory):
            raise UploadDeliveryConflictError("staged file binds another native physical root")
        _verify_bytes(staged_path(directory, fid), row[3], row[4])
        files.append(_staged(row))
    return files


def staged_object(
    conn: Connection[Any], agent_id: int, batch_id: str, ordinal: int, *, unit: UnitIdentity
) -> tuple[Path, StagedFile]:
    row = conn.execute(
        "SELECT s.file_id, s.request_hash, s.name, s.size, s.sha256, s.content_type, s.state, "
        "s.storage_directory, s.source_unit FROM upload_delivery_batches b "
        "JOIN upload_staged_files s ON s.file_id = (b.manifest->'objects'->%s->>'staged_id') "
        "WHERE b.agent_id=%s AND b.batch_id=%s AND b.protocol=2 AND b.ready_at IS NOT NULL "
        "AND s.batch_id = b.batch_id",
        (ordinal, agent_id, batch_id),
    ).fetchone()
    if row is None or row[6] != "ready":
        raise UploadDeliveryConflictError("source object is not available for this batch")
    _own(row[8], unit)
    path = staged_path(Path(row[7]), row[0])
    _verify_bytes(path, row[3], row[4])
    return path, _staged(row)
