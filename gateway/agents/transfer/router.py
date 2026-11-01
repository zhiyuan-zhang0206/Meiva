# meiva:note
# Gateway coordinator for `POST /api/agents/{agent_id}/transfer` and the internal bundle-pull route
# `GET /api/internal/agent-transfers/{transfer_id}/bundle`.
#
# Orchestrates precheck, step-1 termination on from_machine when idling, target prepare, source
# export while terminated, request-scoped bundle staging and target import, atomic
# `SELECT ... FOR UPDATE` Postgres cutover (with target rollback on CAS conflict), step-3 wake
# (`resurrect-explicit`) on to_machine with completion message, and post-commit workspace retirement.
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
from psycopg_pool import ConnectionPool

from base import telemetry
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
    check_agent_state,
    check_blocking_activity,
    compose_transfer_wake_message,
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
    ResurrectAgentRequest,
    TerminateAgentRequest,
    TerminateAgentResponse,
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
_FINALIZE_TIMEOUT_S = 15.0
_EXPORT_TIMEOUT_S = 90.0
_IMPORT_TIMEOUT_S = 90.0


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
    query = (
        "SELECT machine, status, harness FROM agents_meta WHERE id = %s FOR UPDATE"
        if for_update
        else "SELECT machine, status, harness FROM agents_meta WHERE id = %s"
    )
    cur.execute(query, (agent_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return AgentRowFacts(
        agent_id=agent_id, machine=str(row[0]), status=str(row[1]), harness=str(row[2])
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
    lc = cur.fetchone()
    pending_lc = PendingLifecycleFact(kind=str(lc[0]), status=str(lc[1])) if lc else None

    cur.execute(
        "SELECT session_id, status, delta_version > applied_version, handoff_applied_at IS NULL "
        "FROM agent_impersonations WHERE agent_id = %s AND ("
        "  status IN ('requested', 'accepted', 'active') "
        "  OR delta_version > applied_version OR handoff_applied_at IS NULL"
        ") ORDER BY session_id DESC LIMIT 1",
        (agent_id,),
    )
    imp = cur.fetchone()
    imp_conflict = (
        ImpersonationConflictFact(
            session_id=int(imp[0]),
            status=str(imp[1]),
            delta_unapplied=bool(imp[2]),
            handoff_unapplied=bool(imp[3]),
        )
        if imp
        else None
    )

    cur.execute(
        "SELECT batch_id, state FROM upload_delivery_batches "
        "WHERE agent_id = %s AND state IN ('receiving', 'pending') ORDER BY batch_id LIMIT 1",
        (agent_id,),
    )
    up = cur.fetchone()
    pending_up = PendingUploadFact(batch_id=str(up[0]), state=str(up[1])) if up else None
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
    return MachineFacts(
        name=name,
        gateway_url=row[0],
        roles=tuple(str(r) for r in (row[1] or ())),
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
        return evaluate_precheck(
            TransferPrecheckFacts(
                agent=agent,
                agent_id=agent_id,
                to_machine=to_machine,
                pending_lifecycle=pending_lc,
                impersonation=imp_conflict,
                pending_upload=pending_up,
                source_machine=_query_machine_facts(cur, agent.machine),
                target_machine=_query_machine_facts(cur, to_machine),
            )
        )


def _guard_idling_pre_termination_blocking(
    pool: ConnectionPool, agent_id: int, from_machine: str
) -> list[str]:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        agent = _query_agent_facts(cur, agent_id, for_update=True)
        if agent is None:
            raise HTTPException(404, f"agent {agent_id} does not exist")
        if agent.machine != from_machine:
            raise HTTPException(
                409,
                f"agent {agent_id} moved from {from_machine!r} to {agent.machine!r} during transfer",
            )
        state_refusal = check_agent_state(agent)
        if isinstance(state_refusal, TransferRefusal):
            raise HTTPException(state_refusal.status_code, state_refusal.detail)

        pending_lc, imp_conflict, pending_up = _query_blocking_facts(cur, agent_id)
        activity_refusal = check_blocking_activity(
            TransferPrecheckFacts(
                agent=agent,
                agent_id=agent_id,
                to_machine="",
                pending_lifecycle=pending_lc,
                impersonation=imp_conflict,
                pending_upload=pending_up,
            )
        )
        if activity_refusal is not None:
            raise HTTPException(activity_refusal.status_code, activity_refusal.detail)

        cur.execute(
            "SELECT name FROM agent_pages WHERE agent_id = %s AND closed_at IS NULL",
            (agent_id,),
        )
        open_pages = [str(r[0]) for r in cur.fetchall()]
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'user', "
            "heartbeat_paused_until = NULL WHERE id = %s",
            (agent_id,),
        )
        return open_pages


def _drop_killed_shell_ttls(pool: ConnectionPool, agent_id: int, killed: list[int]) -> None:
    with write_transaction(pool) as conn:
        conn.execute(
            "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = ANY(%s)",
            (agent_id, killed),
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
                timeout=httpx.Timeout(_EXPORT_TIMEOUT_S, connect=5.0), trust_env=False
            ) as client,
            client.stream(
                "POST", url, json=export_req.model_dump(mode="json"), headers=headers
            ) as response,
        ):
            if response.status_code != 200:
                raw_err = (await response.aread()).decode("utf-8", errors="replace").strip()
                detail = raw_err or f"source workspace export returned HTTP {response.status_code}"
                status = response.status_code if response.status_code in (409, 413, 422) else 503
                raise HTTPException(status, detail)
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BUNDLE_STREAM_BYTES:
                    raise HTTPException(
                        413, f"workspace export stream exceeds {MAX_BUNDLE_STREAM_BYTES}-byte limit"
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
        bundle_bytes, expected_agent_id=agent_id, expected_transfer_id=transfer_id
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
    step1_closed_pages: list[str] | None = None,
    step1_killed_shells: list[int] | None = None,
) -> tuple[list[str], list[int], telemetry.Event]:
    s1_closed = step1_closed_pages or []
    s1_shells = step1_killed_shells or []
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

        cutover_closed_pages = close_all_agent_pages_in_transaction(conn, snapshot.agent_id)
        closed_pages = list(dict.fromkeys(s1_closed + cutover_closed_pages))
        killed_shell_sessions = sorted(set(s1_shells + manifest.killed_shell_sessions))

        # meiva:note
        # Unlink only pages tied to the current status_changed_at so resurrect on to_machine
        # does not reopen host-local pages from from_machine, preserving older history.
        cur.execute(
            "UPDATE agent_pages SET closed_at = closed_at - interval '1 millisecond' "
            "WHERE agent_id = %s AND closed_at = ("
            "  SELECT status_changed_at FROM agents_meta WHERE id = %s"
            ")",
            (snapshot.agent_id, snapshot.agent_id),
        )
        cur.execute("DELETE FROM agent_shell_ttls WHERE agent_id = %s", (snapshot.agent_id,))
        cur.execute(
            "UPDATE agents_meta SET machine = %s, runtime_owner = NULL, "
            "runtime_generation = NULL, liveness_state = 'unknown', last_probe_at = NULL "
            "WHERE id = %s",
            (body.to_machine, snapshot.agent_id),
        )

        prepared_event = record_audit(
            conn,
            prepare_event_log(
                event_type="transfer",
                agent_id=snapshot.agent_id,
                source=body.source,
                payload={
                    "from_machine": snapshot.from_machine,
                    "to_machine": body.to_machine,
                    "files_transferred": manifest.files_count,
                    "bytes_transferred": manifest.total_file_bytes,
                    "killed_shell_sessions": killed_shell_sessions,
                    "closed_pages": closed_pages,
                },
            ),
        )
    return closed_pages, killed_shell_sessions, prepared_event


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
                agent_id=agent_id, transfer_id=transfer_id, role=role
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
            payload = WorkspaceTransferImportPayload(
                agent_id=agent_id,
                transfer_id=transfer_id,
                bundle_path=bundle_path,
                bundle_bytes=len(bundle_bytes),
                bundle_sha256=bundle_sha256,
            ).model_dump(mode="json")
            raw_import = await deps.rpc.dispatch_to_machine(
                deps.db,
                target_machine=to_machine,
                kind="workspace_transfer_import",
                payload=payload,
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
            raise HTTPException(413 if reason == "transfer_too_large" else 502, err_msg) from exc

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


async def _terminate_source_if_idling(
    deps: TransferDeps,
    *,
    agent_id: int,
    snapshot: AgentTransferSnapshot,
    source: str,
) -> tuple[list[int], list[str]]:
    if snapshot.status != "idling":
        return [], []
    step1_closed_pages = await asyncio.to_thread(
        _guard_idling_pre_termination_blocking,
        deps.db_pool,
        agent_id,
        snapshot.from_machine,
    )
    try:
        raw_term = await deps.rpc.dispatch_to_machine(
            deps.db,
            target_machine=snapshot.from_machine,
            kind="lifecycle",
            payload={
                "path": f"/api/agents/{agent_id}/terminate",
                "body": TerminateAgentRequest(
                    source=source, force=True, kill_all_shell_sessions=True
                ).model_dump(mode="json"),
            },
            timeout_s=_PREPARE_TIMEOUT_S,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(
            503, f"source machine {snapshot.from_machine!r} is unreachable: {exc}"
        ) from exc
    except ClusterOpFailed as exc:
        raise HTTPException(
            502,
            f"source machine {snapshot.from_machine!r} failed to terminate agent: {exc.result.get('error')}",
        ) from exc

    term_response = TerminateAgentResponse.model_validate(raw_term)
    step1_killed_shells: list[int] = []
    if term_response.shell_sessions is not None and term_response.shell_sessions.killed:
        step1_killed_shells = term_response.shell_sessions.killed
        await asyncio.to_thread(
            _drop_killed_shell_ttls, deps.db_pool, agent_id, step1_killed_shells
        )
    return step1_killed_shells, step1_closed_pages


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

    step1_killed_shells, step1_closed_pages = await _terminate_source_if_idling(
        deps,
        agent_id=agent_id,
        snapshot=snapshot,
        source=body.source,
    )

    try:
        await deps.rpc.dispatch_to_machine(
            deps.db,
            target_machine=decision.to_machine,
            kind="workspace_transfer_prepare",
            payload=WorkspaceTransferPreparePayload(
                agent_id=agent_id, transfer_id=transfer_id
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

    export_req = WorkspaceExportRequest(agent_id=agent_id, transfer_id=transfer_id)
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
        closed_pages, killed_shell_sessions, prepared_event = await asyncio.to_thread(
            commit_cutover_blocking,
            deps.db_pool,
            snapshot=snapshot,
            body=body,
            manifest=manifest,
            step1_closed_pages=step1_closed_pages,
            step1_killed_shells=step1_killed_shells,
        )
    except CutoverConflictError as exc:
        await _abort_target_best_effort(
            deps, to_machine=decision.to_machine, agent_id=agent_id, transfer_id=transfer_id
        )
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await _abort_target_best_effort(
            deps, to_machine=decision.to_machine, agent_id=agent_id, transfer_id=transfer_id
        )
        raise

    telemetry.emit_prepared(prepared_event)
    await publish_agent_updated(deps.bus, agent_id)
    for page_name in closed_pages:
        await publish_page_closed(deps.bus, agent_id, page_name)

    wake_message = compose_transfer_wake_message(
        from_machine=snapshot.from_machine,
        to_machine=decision.to_machine,
        source=body.source,
        message=body.message,
        killed_shell_sessions=killed_shell_sessions,
        closed_pages=closed_pages,
    )

    try:
        try:
            await deps.rpc.dispatch_to_machine(
                deps.db,
                target_machine=decision.to_machine,
                kind="lifecycle",
                payload={
                    "path": f"/api/agents/{agent_id}/resurrect-explicit",
                    "body": ResurrectAgentRequest(
                        resurrected_by=body.source,
                        prompt=wake_message,
                    ).model_dump(mode="json"),
                },
            )
        except ClusterOpUnreachable as exc:
            raise HTTPException(
                503,
                f"target machine {decision.to_machine!r} is unreachable for resurrection: {exc}",
            ) from exc
        except ClusterOpFailed as exc:
            raise HTTPException(
                502,
                f"target machine {decision.to_machine!r} failed to resurrect agent: {exc.result.get('error')}",
            ) from exc
    finally:
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
        agent_status="idling",
        files_transferred=manifest.files_count,
        bytes_transferred=manifest.total_file_bytes,
        killed_shell_sessions=killed_shell_sessions,
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
