# meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from base.agents.inbox.chat_delivery import insert_chat_inbound_in_transaction
from base.agents.messages.inbound_provenance import InboundProvenance
from base.agents.upload_delivery.models import (
    PROTOCOL,
    Acceptance,
    CopyProof,
    DeliveryStatus,
    FrozenMessage,
    Manifest,
    Object,
    ReceiveRequest,
    StagedFile,
    UploadDeliveryConflictError,
    current_unit,
    request_hash,
)
from base.agents.upload_delivery.naming import delivered_names, send_directory, upload_note
from base.agents.upload_delivery.paths import agent_upload_dir
from base.agents.upload_delivery.staging import claim_for_batch, staged_object
from base.agents.upload_delivery.storage import lock_agent
from base.api_contracts.agent_contract import AgentNotFound
from base.db.transaction import ConnectionSource, write_transaction
from base.host.db_authority.unit import UnitIdentity
from base.paths import WORKSPACES_DIR_NAME

MAX_COPY_ATTEMPTS = 3


def target_unit(conn: Connection[Any], agent_id: int) -> UnitIdentity:
    if (
        conn.execute("SELECT id FROM agents WHERE id=%s FOR NO KEY UPDATE", (agent_id,)).fetchone()
        is None
    ):
        raise AgentNotFound(agent_id)
    agent = conn.execute(
        "SELECT machine FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,)
    ).fetchone()
    if agent is None:
        raise AgentNotFound(agent_id)
    rows = conn.execute(
        "SELECT u.machine_name, u.home FROM machine_units u JOIN machines m "
        "ON m.name=u.machine_name WHERE m.name=%s AND u.serve_agent_runner "
        "AND u.url=m.gateway_url FOR SHARE OF u,m",
        (agent[0],),
    ).fetchall()
    if len(rows) != 1:
        raise UploadDeliveryConflictError("target has no unique registered serving runner unit")
    return UnitIdentity(machine=rows[0][0], home=rows[0][1])


def _lookup(conn: Connection[Any], key: str, fingerprint: str) -> Acceptance | None:
    row = conn.execute(
        "SELECT request_hash, protocol, acceptance FROM upload_delivery_batches "
        "WHERE operation_key=%s",
        (key,),
    ).fetchone()
    if row is None:
        return None
    if row[0] != fingerprint or row[1] != PROTOCOL or row[2] is None:
        raise UploadDeliveryConflictError(
            "idempotency key already identifies another upload manifest"
        )
    return Acceptance.model_validate(row[2])


def _taken_directories(conn: Connection[Any], agent_id: int, prefix: str) -> set[str]:
    rows = conn.execute(
        "SELECT send_directory FROM upload_delivery_batches "
        "WHERE agent_id=%s AND send_directory LIKE %s",
        (agent_id, f"{prefix}%"),
    ).fetchall()
    return {str(row[0]) for row in rows if row[0] is not None}


def send(
    pool: ConnectionSource,
    key: str,
    agent_id: int,
    *,
    file_ids: list[str],
    text: str,
    provenance: InboundProvenance,
    sent_at: datetime,
    batch_id: str,
    local_machine: str,
    home: Path,
    upload_root: Path,
) -> Acceptance:
    frozen_message = FrozenMessage(text=text, source="user")
    fingerprint = request_hash(agent_id, file_ids, frozen_message.text)
    source = current_unit(local_machine=local_machine, home=home)
    directory = agent_upload_dir(agent_id, create=False, root=upload_root).resolve()
    with write_transaction(pool) as conn:
        lock_agent(conn, agent_id)
        found = _lookup(conn, key, fingerprint)
        if found is not None:
            return found
        target = target_unit(conn, agent_id)
        staged = claim_for_batch(conn, agent_id, file_ids, unit=source, directory=directory)
        base_dir = sent_at.strftime("%Y%m%d-%H%M%S")
        send_dir = send_directory(sent_at, _taken_directories(conn, agent_id, base_dir))
        names = delivered_names(item.name for item in staged)
        objects = [
            Object(
                ordinal=ordinal,
                name=name,
                staged_id=item.file_id,
                size=item.size,
                sha256=item.sha256,
                content_type=item.content_type,
            )
            for ordinal, (item, name) in enumerate(zip(staged, names, strict=True))
        ]
        manifest = Manifest(
            batch_id=batch_id,
            agent_id=agent_id,
            sent_at=sent_at.isoformat(),
            directory=send_dir,
            objects=objects,
        )
        acceptance = Acceptance(
            batch_id=manifest.batch_id,
            agent_id=agent_id,
            directory=manifest.directory,
            files=manifest.objects,
            status_url=f"/api/keyed/v1/agents/{agent_id}/uploads/{manifest.batch_id}",
        )
        conn.execute(
            "INSERT INTO upload_delivery_batches "
            "(batch_id, operation_key, agent_id, request_hash, manifest, source_unit, target_unit, "
            "storage_machine, storage_directory, provenance, acceptance, ready_at, state, "
            "protocol, send_directory, message) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),'pending',%s,%s,%s)",
            (
                manifest.batch_id,
                key,
                agent_id,
                fingerprint,
                Jsonb(manifest.model_dump()),
                Jsonb(source.model_dump()),
                Jsonb(target.model_dump()),
                source.machine,
                str(directory),
                Jsonb(
                    {
                        "source_verified_by": provenance.source_verified_by,
                        "source_transport": provenance.source_transport,
                    }
                ),
                Jsonb(acceptance.model_dump()),
                PROTOCOL,
                manifest.directory,
                Jsonb(frozen_message.model_dump()),
            ),
        )
        conn.execute(
            "UPDATE upload_staged_files SET batch_id=%s WHERE file_id = ANY(%s)",
            (manifest.batch_id, file_ids),
        )
        return acceptance


def status(pool: ConnectionSource, agent_id: int, batch_id: str) -> DeliveryStatus:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT batch_id, state, reason, send_directory, message, manifest, inbound_id, "
            "source_unit, target_unit, attempts, next_attempt_at, protocol "
            "FROM upload_delivery_batches WHERE agent_id=%s AND batch_id=%s",
            (agent_id, batch_id),
        ).fetchone()
    if row is None or row[11] != PROTOCOL:
        raise AgentNotFound(agent_id)
    manifest = Manifest.model_validate(row[5])
    frozen = FrozenMessage.model_validate(row[4])
    return DeliveryStatus(
        batch_id=row[0],
        state=row[1],
        reason=row[2],
        directory=row[3],
        message_text=frozen.text,
        files=manifest.objects,
        inbound_id=row[6],
        source=UnitIdentity.model_validate(row[7]),
        target=UnitIdentity.model_validate(row[8]),
        attempts=row[9],
        next_attempt_at=row[10] if row[1] == "pending" else None,
    )


def served_object(
    pool: ConnectionSource,
    agent_id: int,
    batch_id: str,
    ordinal: int,
    *,
    local_machine: str,
    home: Path,
) -> tuple[Path, StagedFile]:
    unit = current_unit(local_machine=local_machine, home=home)
    with pool.connection() as conn:
        return staged_object(conn, agent_id, batch_id, ordinal, unit=unit)


def accepted_object(
    pool: ConnectionSource, agent_id: int, batch_id: str, ordinal: int
) -> tuple[UnitIdentity, str, Object]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT target_unit, manifest FROM upload_delivery_batches "
            "WHERE agent_id=%s AND batch_id=%s AND protocol=%s AND state='accepted'",
            (agent_id, batch_id, PROTOCOL),
        ).fetchone()
    if row is None:
        raise AgentNotFound(agent_id)
    manifest = Manifest.model_validate(row[1])
    if not 0 <= ordinal < len(manifest.objects):
        raise UploadDeliveryConflictError("object ordinal is absent from the fixed manifest")
    return UnitIdentity.model_validate(row[0]), manifest.directory, manifest.objects[ordinal]


# meiva:note
# The claim is the SELECT's own UPDATE: every returned row has its next_attempt_at
# pushed claim_seconds ahead, so a second worker reading at the same instant finds
# nothing (task #5439 — two gateways on one database copied the same batch twice).
# FOR UPDATE SKIP LOCKED keeps concurrent claims from waiting on each other; a failed
# copy overwrites the claim with its own backoff (record_failure), and a worker that
# dies mid-copy costs that batch one claim window and nothing else.
# meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md
def due(
    pool: ConnectionSource,
    limit: int = 8,
    *,
    local_machine: str,
    home: Path,
    claim_seconds: float,
) -> list[ReceiveRequest]:
    with write_transaction(pool) as conn:
        rows = conn.execute(
            "UPDATE upload_delivery_batches SET next_attempt_at=now()+make_interval(secs => %s) "
            "WHERE batch_id IN ("
            "SELECT batch_id FROM upload_delivery_batches "
            "WHERE state='pending' AND protocol=%s AND next_attempt_at<=now() AND source_unit=%s "
            "ORDER BY next_attempt_at, batch_id LIMIT %s FOR UPDATE SKIP LOCKED"
            ") RETURNING manifest, source_unit, target_unit",
            (
                claim_seconds,
                PROTOCOL,
                Jsonb(current_unit(local_machine=local_machine, home=home).model_dump()),
                limit,
            ),
        ).fetchall()
    return [
        ReceiveRequest(
            version=PROTOCOL,
            manifest=Manifest.model_validate(row[0]),
            source=UnitIdentity.model_validate(row[1]),
            target=UnitIdentity.model_validate(row[2]),
        )
        for row in rows
    ]


def validate_pending(pool: ConnectionSource, request: ReceiveRequest) -> bool:
    """Fence known placement changes before copy; final acceptance rechecks under lock."""
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT state FROM upload_delivery_batches WHERE batch_id=%s AND protocol=%s FOR UPDATE",
            (request.manifest.batch_id, PROTOCOL),
        ).fetchone()
        if row is None or row[0] != "pending":
            return False
        if target_unit(conn, request.manifest.agent_id) != request.target:
            raise UploadDeliveryConflictError("agent placement changed after upload acceptance")
        return True


def record_failure(pool: ConnectionSource, batch_id: str, reason: str, *, hold: bool) -> str | None:
    with write_transaction(pool) as conn:
        row = conn.execute(
            "UPDATE upload_delivery_batches SET reason=%s, attempts=attempts+1, "
            "state=CASE WHEN %s OR attempts+1 >= %s THEN 'hold' ELSE 'pending' END, "
            "next_attempt_at=now()+interval '30 seconds' * (attempts+1) "
            "WHERE batch_id=%s AND protocol=%s AND state='pending' RETURNING state",
            (reason, hold, MAX_COPY_ATTEMPTS, batch_id, PROTOCOL),
        ).fetchone()
    return None if row is None else row[0]


# meiva:note
# The received files' own namespace inside the agent's workspace:
# `<target home>/workspaces/<agent id>/<configured name>/<send directory>/<object>`. The home,
# the workspaces root, the agent's own directory and the send directory are all pinned to
# what the batch froze, so a proof can only name a file in the workspace of the machine the batch
# targets. The one free segment is the delivered-directory name (`MEIVA_UPLOAD_DIR_NAME`): a
# receiver whose config lags the gateway still delivered a real, readable file, and refusing that
# proof would turn a stale config into a batch nobody can accept. `..` is refused outright — as
# that free segment it would put the namespace beside the workspace instead of inside it.
def _proof_paths(proof: CopyProof, manifest: Manifest, *, home: str) -> list[str]:
    native_directory = PurePosixPath(proof.directory)
    workspace = PurePosixPath(home) / WORKSPACES_DIR_NAME / str(manifest.agent_id)
    if (
        ".." in native_directory.parts
        or native_directory.name != manifest.directory
        or native_directory.parent.parent != workspace
    ):
        raise UploadDeliveryConflictError(
            "copy proof directory differs from its fixed agent batch namespace"
        )
    return [str(native_directory / item.name) for item in manifest.objects]


def complete(pool: ConnectionSource, request: ReceiveRequest, proof: CopyProof) -> int:
    if (
        proof.version != PROTOCOL
        or proof.target != request.target
        or proof.manifest_hash != request.manifest.fingerprint()
    ):
        raise UploadDeliveryConflictError("receiver proof differs from the frozen target manifest")
    manifest = request.manifest
    with write_transaction(pool) as conn:
        row = conn.execute(
            "SELECT inbound_id, target_unit, manifest, provenance, state, source_unit, message "
            "FROM upload_delivery_batches "
            "WHERE batch_id=%s AND protocol=%s AND ready_at IS NOT NULL FOR UPDATE",
            (manifest.batch_id, PROTOCOL),
        ).fetchone()
        if row is None:
            raise UploadDeliveryConflictError("source acceptance is absent")
        if (
            UnitIdentity.model_validate(row[1]) != request.target
            or Manifest.model_validate(row[2]) != manifest
            or UnitIdentity.model_validate(row[5]) != request.source
        ):
            raise UploadDeliveryConflictError("stored intent differs from receiver proof")
        if row[0] is not None:
            return row[0]
        if row[4] != "pending":
            raise UploadDeliveryConflictError("held delivery cannot accept a late copy proof")
        if target_unit(conn, manifest.agent_id) != request.target:
            raise UploadDeliveryConflictError("agent placement changed after upload acceptance")
        paths = _proof_paths(proof, manifest, home=request.target.home)
        frozen = FrozenMessage.model_validate(row[6])
        provenance = InboundProvenance(**row[3])
        note_content = upload_note(
            request.target.machine,
            [
                (item.name, item.content_type, item.size, path)
                for item, path in zip(manifest.objects, paths, strict=True)
            ],
        )
        upload_files = [
            {
                "name": item.name,
                "size": item.size,
                "content_type": item.content_type,
                "path": path,
                "batch_id": manifest.batch_id,
                "ordinal": item.ordinal,
            }
            for item, path in zip(manifest.objects, paths, strict=True)
        ]
        note_receipt, _ = insert_chat_inbound_in_transaction(
            conn,
            agent_id=manifest.agent_id,
            content=note_content,
            source="system:upload",
            payload={"upload_files": upload_files},
            client_message_id=f"upload-delivery-v2:{manifest.batch_id}:note",
            provenance=provenance,
        )
        user_content = frozen.text if frozen.text.strip() else "[files]"
        user_receipt, _ = insert_chat_inbound_in_transaction(
            conn,
            agent_id=manifest.agent_id,
            content=user_content,
            source=frozen.source,
            payload=None,
            client_message_id=f"upload-delivery-v2:{manifest.batch_id}:message",
            provenance=provenance,
        )
        conn.execute(
            "UPDATE upload_delivery_batches SET state='accepted', reason=NULL, inbound_id=%s, "
            "copy_proof=%s, outcome=%s WHERE batch_id=%s",
            (
                user_receipt.inbound_id,
                Jsonb(proof.model_dump()),
                Jsonb(
                    {
                        "note_inbound_id": note_receipt.inbound_id,
                        "message_inbound_id": user_receipt.inbound_id,
                        "content": note_content,
                        "message_text": user_content,
                    }
                ),
                manifest.batch_id,
            ),
        )
        return user_receipt.inbound_id
