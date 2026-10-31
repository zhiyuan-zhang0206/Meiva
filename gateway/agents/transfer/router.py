# meiva:note
# Gateway coordinator for `POST /api/agents/{agent_id}/transfer` and the internal bundle-pull route
# `GET /api/internal/agent-transfers/{transfer_id}/bundle`.
#
# Orchestrates precheck, target prepare, source fenced export, request-scoped bundle staging and
# target import, atomic `SELECT ... FOR UPDATE` Postgres cutover (with target rollback on CAS
# conflict), and post-commit workspace retirement on source and target.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.messages.inbound import PASSIVE_INBOUND_KEY
from base.agents.messages.kwargs import NoteTag
from base.clock.ids import new_uuid
from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.telemetry.audit_events import prepare_event_log, record_audit
from gateway.agents.transfer.bundle_store import TransferBundleStore
from gateway.agents.transfer.transfer_plan import (
    AgentRowFacts,
    AgentTransferSnapshot,
    ImpersonationConflictFact,
    MachineFacts,
    PendingLifecycleFact,
    PendingUploadFact,
    TransferPlan,
    TransferPrecheckFacts,
    TransferRefusal,
    compose_transfer_system_note,
    evaluate_precheck,
    verify_cutover_cas,
)
from gateway.http.config import gateway_rpc
from ops.agent_transfer import (
    MAX_BUNDLE_STREAM_BYTES,
    WorkspaceTransferCorruptError,
    WorkspaceTransferTooLargeError,
    decode_bundle,
)
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable, RpcClient
from ops.lifecycle.events import publish_page_closed
from ops.pages import close_all_agent_pages_in_transaction
from ops.rpc_schemas import (
    TransferAgentRequest,
    TransferAgentResponse,
    WorkspaceBundleManifest,
    WorkspaceExportRequest,
    WorkspaceTransferAbortPayload,
    WorkspaceTransferFinalizePayload,
    WorkspaceTransferImportPayload,
    WorkspaceTransferImportResult,
    WorkspaceTransferPreparePayload,
)

router = APIRouter()
_log = logging.getLogger(__name__)

_PREPARE_TIMEOUT_S = 15.0
_EXPORT_TIMEOUT_S = 90.0
_IMPORT_TIMEOUT_S = 90.0
_FINALIZE_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class TransferDeps:
    db_pool: ConnectionPool
    db: Database
    rpc: RpcClient
    bus: EventBus
    bundles: TransferBundleStore
    access: GatewayAccess
    rng: random.Random


def _deps_from_request(request: Request) -> TransferDeps:
    state = request.app.state
    return TransferDeps(
        db_pool=state.db_pool,
        db=state.db,
        rpc=gateway_rpc(request),
        bus=state.bus,
        bundles=state.agent_transfer_bundles,
        access=state.gateway_access,
        rng=state.gateway_config.rng,
    )


def _query_agent_facts(
    cur: psycopg.Cursor, agent_id: int, *, for_update: bool
) -> AgentRowFacts | None:
    if for_update:
        cur.execute(
            "SELECT machine, status, harness, runtime_owner, runtime_generation, "
            "last_admission_at, session_index FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
    else:
        cur.execute(
            "SELECT machine, status, harness, runtime_owner, runtime_generation, "
            "last_admission_at, session_index FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
    row = cur.fetchone()
    if row is None:
        return None
    return AgentRowFacts(
        agent_id=agent_id,
        machine=str(row[0]),
        status=str(row[1]),
        harness=str(row[2]),
        runtime_owner=row[3],
        runtime_generation=row[4],
        last_admission_at=row[5],
        session_index=int(row[6]),
    )


def _query_blocking_facts(
    cur: psycopg.Cursor, agent_id: int
) -> tuple[PendingLifecycleFact | None, ImpersonationConflictFact | None, PendingUploadFact | None]:
    cur.execute(
        "SELECT kind, status FROM inbound_messages "
        "WHERE agent_id = %s AND kind IN ('restart', 'terminate') "
        "AND status IN ('pending', 'claimed') ORDER BY id LIMIT 1",
        (agent_id,),
    )
    lc_row = cur.fetchone()
    pending_lc = (
        PendingLifecycleFact(kind=str(lc_row[0]), status=str(lc_row[1]))
        if lc_row is not None
        else None
    )

    cur.execute(
        "SELECT session_id, status, delta_version > applied_version, handoff_applied_at IS NULL "
        "FROM agent_impersonations "
        "WHERE agent_id = %s AND ("
        "  status IN ('requested', 'accepted', 'active') "
        "  OR delta_version > applied_version "
        "  OR handoff_applied_at IS NULL"
        ") ORDER BY session_id DESC LIMIT 1",
        (agent_id,),
    )
    imp_row = cur.fetchone()
    imp_conflict = (
        ImpersonationConflictFact(
            session_id=int(imp_row[0]),
            status=str(imp_row[1]),
            delta_unapplied=bool(imp_row[2]),
            handoff_unapplied=bool(imp_row[3]),
        )
        if imp_row is not None
        else None
    )

    cur.execute(
        "SELECT batch_id, state FROM upload_delivery_batches "
        "WHERE agent_id = %s AND state IN ('receiving', 'pending') "
        "ORDER BY batch_id LIMIT 1",
        (agent_id,),
    )
    up_row = cur.fetchone()
    pending_up = (
        PendingUploadFact(batch_id=str(up_row[0]), state=str(up_row[1]))
        if up_row is not None
        else None
    )
    return pending_lc, imp_conflict, pending_up


def _query_machine_facts(cur: psycopg.Cursor, name: str) -> MachineFacts | None:
    cur.execute(
        "SELECT gateway_url, role, paused_at IS NOT NULL, stopped_at IS NOT NULL "
        "FROM machines WHERE name = %s",
        (name,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    roles = tuple(str(r) for r in (row[1] or ()))
    return MachineFacts(
        name=name,
        gateway_url=row[0],
        roles=roles,
        paused=bool(row[2]),
        stopped=bool(row[3]),
    )


def _precheck_blocking(
    pool: ConnectionPool, agent_id: int, to_machine: str
) -> TransferPlan | TransferRefusal:
    with pool.connection() as conn, conn.cursor() as cur:
        agent = _query_agent_facts(cur, agent_id, for_update=False)
        if agent is None:
            return evaluate_precheck(
                TransferPrecheckFacts(agent=None, agent_id=agent_id, to_machine=to_machine)
            )
        pending_lc, imp_conflict, pending_up = _query_blocking_facts(cur, agent_id)
        source_machine = _query_machine_facts(cur, agent.machine)
        target_machine = _query_machine_facts(cur, to_machine)
        return evaluate_precheck(
            TransferPrecheckFacts(
                agent=agent,
                agent_id=agent_id,
                to_machine=to_machine,
                pending_lifecycle=pending_lc,
                impersonation=imp_conflict,
                pending_upload=pending_up,
                source_machine=source_machine,
                target_machine=target_machine,
            )
        )


async def _fetch_source_export(
    source_ops_url: str,
    export_req: WorkspaceExportRequest,
    headers: dict[str, str],
) -> bytes:
    url = f"{source_ops_url.rstrip('/')}/workspace/export"
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(_EXPORT_TIMEOUT_S, connect=5.0),
                trust_env=False,
            ) as client,
            client.stream(
                "POST",
                url,
                json=export_req.model_dump(mode="json"),
                headers=headers,
            ) as response,
        ):
            if response.status_code != 200:
                raw_err = (await response.aread()).decode("utf-8", errors="replace").strip()
                detail = raw_err or f"source workspace export returned HTTP {response.status_code}"
                if response.status_code in (409, 413, 422):
                    raise HTTPException(response.status_code, detail)
                raise HTTPException(503, detail)
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BUNDLE_STREAM_BYTES:
                    raise HTTPException(
                        413,
                        f"workspace export stream exceeds {MAX_BUNDLE_STREAM_BYTES}-byte limit",
                    )
            return bytes(buf)
    except httpx.HTTPError as exc:
        raise HTTPException(
            503, f"source machine failed to export workspace at {url}: {exc}"
        ) from exc


def _decode_and_digest_bundle(
    bundle_bytes: bytes, *, agent_id: int, transfer_id: str
) -> tuple[WorkspaceBundleManifest, str]:
    manifest, _tar = decode_bundle(
        bundle_bytes,
        expected_agent_id=agent_id,
        expected_transfer_id=transfer_id,
    )
    return manifest, hashlib.sha256(bundle_bytes).hexdigest()


class CutoverConflictError(Exception):
    pass


# meiva:note
# Atomically commit the cross-machine ownership cutover under `SELECT ... FOR UPDATE` on
# `agents_meta`, failing closed with `CutoverConflictError` if any fenced attribute or blocking
# relation changed while the workspace bundle was transferred.
def commit_cutover_blocking(
    pool: ConnectionPool,
    *,
    snapshot: AgentTransferSnapshot,
    body: TransferAgentRequest,
    manifest: WorkspaceBundleManifest,
) -> tuple[list[str], telemetry.Event]:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT paused_at IS NOT NULL, stopped_at IS NOT NULL, 'agent-runner' = ANY(role) "
            "FROM machines WHERE name = %s FOR SHARE",
            (body.to_machine,),
        )
        target_row = cur.fetchone()
        if target_row is None or not target_row[2]:
            raise CutoverConflictError(
                f"target machine {body.to_machine!r} is no longer an active agent-runner"
            )
        if target_row[0] or target_row[1]:
            raise CutoverConflictError(
                f"target machine {body.to_machine!r} became paused or stopped during transfer"
            )

        current = _query_agent_facts(cur, snapshot.agent_id, for_update=True)
        cas_error = verify_cutover_cas(snapshot, current)
        if cas_error is not None:
            raise CutoverConflictError(cas_error)

        pending_lc, imp_conflict, pending_up = _query_blocking_facts(cur, snapshot.agent_id)
        if pending_lc is not None:
            raise CutoverConflictError(
                f"agent {snapshot.agent_id} queued a {pending_lc.kind!r} command during transfer"
            )
        if imp_conflict is not None:
            raise CutoverConflictError(
                f"agent {snapshot.agent_id} opened an impersonation session during transfer"
            )
        if pending_up is not None:
            raise CutoverConflictError(
                f"agent {snapshot.agent_id} started an upload delivery batch during transfer"
            )

        closed_pages = close_all_agent_pages_in_transaction(conn, snapshot.agent_id)
        cur.execute("DELETE FROM agent_shell_ttls WHERE agent_id = %s", (snapshot.agent_id,))
        cur.execute(
            "UPDATE agents_meta SET machine = %s, runtime_owner = NULL, "
            "runtime_generation = NULL, liveness_state = 'unknown', last_probe_at = NULL "
            "WHERE id = %s",
            (body.to_machine, snapshot.agent_id),
        )

        note_text = compose_transfer_system_note(
            from_machine=snapshot.from_machine,
            to_machine=body.to_machine,
            source=body.source,
            message=body.message,
            killed_shell_sessions=manifest.killed_shell_sessions,
            closed_pages=closed_pages,
        )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', %s, %s)",
            (
                snapshot.agent_id,
                note_text,
                body.source,
                Jsonb({"note_tag": NoteTag.CONTEXT.value, PASSIVE_INBOUND_KEY: True}),
            ),
        )

        audit_payload: dict[str, object] = {
            "from_machine": snapshot.from_machine,
            "to_machine": body.to_machine,
            "files_transferred": manifest.files_count,
            "bytes_transferred": manifest.total_file_bytes,
            "killed_shell_sessions": manifest.killed_shell_sessions,
            "closed_pages": closed_pages,
        }
        event = prepare_event_log(
            event_type="transfer",
            agent_id=snapshot.agent_id,
            source=body.source,
            payload=audit_payload,
        )
        prepared_event = record_audit(conn, event)
    return closed_pages, prepared_event


async def _abort_target_best_effort(
    deps: TransferDeps, *, to_machine: str, agent_id: int, transfer_id: str
) -> None:
    try:
        await deps.rpc.dispatch_to_machine(
            deps.db,
            target_machine=to_machine,
            kind="workspace_transfer_abort",
            payload=WorkspaceTransferAbortPayload(
                agent_id=agent_id, transfer_id=transfer_id
            ).model_dump(mode="json"),
            timeout_s=_FINALIZE_TIMEOUT_S,
            retries=0,
        )
    except Exception:
        _log.warning(
            "best-effort workspace_transfer_abort failed on %s for agent %d",
            to_machine,
            agent_id,
            exc_info=True,
        )


async def _finalize_best_effort(
    deps: TransferDeps,
    *,
    machine: str,
    role: Literal["source", "target"],
    agent_id: int,
    transfer_id: str,
) -> None:
    try:
        await deps.rpc.dispatch_to_machine(
            deps.db,
            target_machine=machine,
            kind="workspace_transfer_finalize",
            payload=WorkspaceTransferFinalizePayload(
                agent_id=agent_id,
                transfer_id=transfer_id,
                role=role,
            ).model_dump(mode="json"),
            timeout_s=_FINALIZE_TIMEOUT_S,
            retries=0,
        )
    except Exception:
        _log.warning(
            "best-effort workspace_transfer_finalize (%s) failed on %s for agent %d",
            role,
            machine,
            agent_id,
            exc_info=True,
        )


async def _stage_and_import_bundle(
    deps: TransferDeps,
    *,
    agent_id: int,
    to_machine: str,
    transfer_id: str,
    bundle_bytes: bytes,
    bundle_sha256: str,
    manifest: WorkspaceBundleManifest,
) -> None:
    bundle_path = f"/api/internal/agent-transfers/{transfer_id}/bundle"
    with deps.bundles.stage(transfer_id, bundle_bytes):
        try:
            raw_import = await deps.rpc.dispatch_to_machine(
                deps.db,
                target_machine=to_machine,
                kind="workspace_transfer_import",
                payload=WorkspaceTransferImportPayload(
                    agent_id=agent_id,
                    transfer_id=transfer_id,
                    bundle_path=bundle_path,
                    bundle_bytes=len(bundle_bytes),
                    bundle_sha256=bundle_sha256,
                ).model_dump(mode="json"),
                timeout_s=_IMPORT_TIMEOUT_S,
                retries=0,
            )
            import_result = WorkspaceTransferImportResult.model_validate(raw_import)
        except ClusterOpUnreachable as exc:
            await _abort_target_best_effort(
                deps, to_machine=to_machine, agent_id=agent_id, transfer_id=transfer_id
            )
            raise HTTPException(
                503, f"target machine {to_machine!r} failed during workspace import: {exc}"
            ) from exc
        except ClusterOpFailed as exc:
            await _abort_target_best_effort(
                deps, to_machine=to_machine, agent_id=agent_id, transfer_id=transfer_id
            )
            reason = exc.result.get("reason")
            err_msg = str(exc.result.get("error") or "workspace import failed")
            status_code = 413 if reason == "transfer_too_large" else 502
            raise HTTPException(status_code, err_msg) from exc

    if (
        import_result.files_transferred != manifest.files_count
        or import_result.bytes_transferred != manifest.total_file_bytes
    ):
        await _abort_target_best_effort(
            deps, to_machine=to_machine, agent_id=agent_id, transfer_id=transfer_id
        )
        raise HTTPException(
            502, "target workspace import counters did not match the verified manifest"
        )


# meiva:note
# Run the complete cross-machine agent transfer over explicit typed `TransferDeps` collaborators.
async def execute_agent_transfer(
    deps: TransferDeps,
    *,
    agent_id: int,
    body: TransferAgentRequest,
) -> TransferAgentResponse:
    decision = await asyncio.to_thread(
        _precheck_blocking,
        deps.db_pool,
        agent_id,
        body.to_machine,
    )
    if isinstance(decision, TransferRefusal):
        raise HTTPException(decision.status_code, decision.detail)

    snapshot = decision.snapshot
    transfer_id = new_uuid(deps.rng).hex

    try:
        await deps.rpc.dispatch_to_machine(
            deps.db,
            target_machine=decision.to_machine,
            kind="workspace_transfer_prepare",
            payload=WorkspaceTransferPreparePayload(
                agent_id=agent_id,
                transfer_id=transfer_id,
            ).model_dump(mode="json"),
            timeout_s=_PREPARE_TIMEOUT_S,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(
            503, f"target machine {decision.to_machine!r} is unreachable: {exc}"
        ) from exc
    except ClusterOpFailed as exc:
        raise HTTPException(
            502,
            f"target machine {decision.to_machine!r} refused workspace prepare: {exc.result.get('error')}",
        ) from exc

    export_req = WorkspaceExportRequest(
        agent_id=agent_id,
        transfer_id=transfer_id,
        expected_status=snapshot.status,
        expected_runtime_owner=snapshot.runtime_owner,
        expected_runtime_generation=snapshot.runtime_generation,
        expected_last_admission_at=snapshot.last_admission_at,
        expected_session_index=snapshot.session_index,
    )
    bundle_bytes = await _fetch_source_export(
        decision.source_ops_url,
        export_req,
        gateway_auth_headers(deps.access),
    )

    try:
        manifest, bundle_sha256 = await asyncio.to_thread(
            _decode_and_digest_bundle,
            bundle_bytes,
            agent_id=agent_id,
            transfer_id=transfer_id,
        )
    except WorkspaceTransferTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except WorkspaceTransferCorruptError as exc:
        raise HTTPException(502, f"source workspace bundle failed verification: {exc}") from exc

    await _stage_and_import_bundle(
        deps,
        agent_id=agent_id,
        to_machine=decision.to_machine,
        transfer_id=transfer_id,
        bundle_bytes=bundle_bytes,
        bundle_sha256=bundle_sha256,
        manifest=manifest,
    )

    try:
        closed_pages, prepared_event = await asyncio.to_thread(
            commit_cutover_blocking,
            deps.db_pool,
            snapshot=snapshot,
            body=body,
            manifest=manifest,
        )
    except CutoverConflictError as exc:
        await _abort_target_best_effort(
            deps,
            to_machine=decision.to_machine,
            agent_id=agent_id,
            transfer_id=transfer_id,
        )
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await _abort_target_best_effort(
            deps,
            to_machine=decision.to_machine,
            agent_id=agent_id,
            transfer_id=transfer_id,
        )
        raise

    telemetry.emit_prepared(prepared_event)
    await publish_agent_updated(deps.bus, agent_id)
    for page_name in closed_pages:
        await publish_page_closed(deps.bus, agent_id, page_name)

    await asyncio.gather(
        _finalize_best_effort(
            deps,
            machine=decision.to_machine,
            role="target",
            agent_id=agent_id,
            transfer_id=transfer_id,
        ),
        _finalize_best_effort(
            deps,
            machine=snapshot.from_machine,
            role="source",
            agent_id=agent_id,
            transfer_id=transfer_id,
        ),
    )

    return TransferAgentResponse(
        agent_id=agent_id,
        from_machine=snapshot.from_machine,
        to_machine=decision.to_machine,
        agent_status=snapshot.status,
        files_transferred=manifest.files_count,
        bytes_transferred=manifest.total_file_bytes,
        killed_shell_sessions=manifest.killed_shell_sessions,
        closed_pages=closed_pages,
    )


@router.get("/api/internal/agent-transfers/{transfer_id}/bundle", include_in_schema=False)
async def get_staged_transfer_bundle(
    transfer_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    request: Request,
) -> Response:
    bundles: TransferBundleStore = request.app.state.agent_transfer_bundles
    content = bundles.get(transfer_id)
    if content is None:
        raise HTTPException(404, "staged transfer bundle not found")
    return Response(content=content, media_type="application/octet-stream")


@router.post(
    "/api/agents/{agent_id}/transfer",
    response_model=TransferAgentResponse,
    description=(
        "Transfer a settled (`idling` or `terminated`) native Meiva agent — including its "
        "workspace directory and central placement — to another live `agent-runner` machine. "
        "Running agents, external-harness agents, and agents with open or unsettled impersonation "
        "leases are refused with HTTP 409."
    ),
)
async def post_agent_transfer(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    body: TransferAgentRequest,
    request: Request,
) -> TransferAgentResponse:
    return await execute_agent_transfer(_deps_from_request(request), agent_id=agent_id, body=body)
