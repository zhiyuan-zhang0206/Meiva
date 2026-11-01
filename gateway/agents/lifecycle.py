from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Body, HTTPException, Request
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from base.agents.birth.harness import coerce_harness
from base.agents.impersonation import ImpersonationError
from base.agents.impersonation.maintenance import force_expire_impersonation
from base.agents.inbox.compact import insert_compact_request_inbound
from base.api_contracts.agent_contract import (
    AgentNotFound,
    AgentStatus,
    Harness,
)
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.agents.launch.forward import forward_to_home_machine
from gateway.http.config import GatewayConfig, gateway_config, gateway_rpc
from gateway.http.model_catalog import require_model_catalog
from ops.cluster.rpc import RpcClient
from ops.rpc_schemas import (
    CancelAgentResponse,
    CompactAgentResponse,
    OpenTaskRow,
    OpenTasksHint,
    RestartAgentRequest,
    RestartAgentResponse,
    ResurrectAgentRequest,
    ResurrectAgentResponse,
    TerminateAgentRequest,
    TerminateAgentResponse,
)

router = APIRouter()

_log = logging.getLogger(__name__)


_OPEN_TASKS_SHOWN = 5


class ForceExpireImpersonationRequest(BaseModel):
    session_id: int = Field(ge=0)


class ForceExpireImpersonationResponse(BaseModel):
    session_id: int
    status: Literal["expired", "not_open"]


@router.post(
    "/api/agents/{agent_id}/impersonation/force-expire",
    description="End the caller's observed takeover without terminating the native agent.",
)
async def post_force_expire_impersonation(
    agent_id: int, body: ForceExpireImpersonationRequest, request: Request
) -> ForceExpireImpersonationResponse:

    verified_by = getattr(request.state, "source_verified_by", None)
    principal = getattr(request.state, "auth_principal", None)
    subject = getattr(principal, "subject", None)
    if verified_by is None or subject is None:
        raise RuntimeError("force-expire reached its handler without a verified principal")
    actor = f"{verified_by}:{subject}"
    try:
        status = await asyncio.to_thread(
            force_expire_impersonation,
            request.app.state.db_pool,
            request.app.state.db,
            request.app.state.bus,
            agent_id,
            body.session_id,
            actor,
        )
    except ImpersonationError as exc:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} not found") from exc
    return ForceExpireImpersonationResponse(session_id=body.session_id, status=status)


@router.post(
    "/api/agents/{agent_id}/terminate",
    description="Terminate through the home runner's durable native control path.\n\nClaim returns to END and the host flushes before applying normal termination.\nAn optional message is committed as pending work before the command, retained\nfor later resurrection without causing another model turn. Force returns\nenqueued while the exact original host settles its task and execution\nresources; acceptance and metadata status are not proof of completed exit.\n\nBoth paths forward to the home runner. A missing agent returns 404; an\nalready-terminated identity is a no-op for graceful termination.\n\n`kill_all_shell_sessions` also kills the agent's shell sessions on its home\nmachine, silently; `shell_sessions` in the response says whether that\nalready happened (`now`, with the killed ids) or happens right before a\ngraceful termination applies (`at_exit`).\n\nOn success the response additionally carries `open_tasks` — the tasks the\nagent still owns (in_progress; at most five, most recently\nupdated first) as it goes down. The hint is advisory: a failed read leaves\nit null and never changes the termination result.",
)
async def post_agent_terminate(
    agent_id: int,
    request: Request,
    body: TerminateAgentRequest = Body(default_factory=TerminateAgentRequest),  # noqa: B008
) -> TerminateAgentResponse:

    return await terminate_agent_with_open_tasks(
        agent_id,
        body,
        request.app.state.db_pool,
        db=request.app.state.db,
        config=request.app.state.gateway_config,
        rpc=gateway_rpc(request),
    )


async def terminate_agent_with_open_tasks(
    agent_id: int,
    body: TerminateAgentRequest,
    pool: ConnectionPool,
    *,
    db: Database,
    config: GatewayConfig,
    rpc: RpcClient,
) -> TerminateAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/terminate",
        body.model_dump(),
        db=db,
        pool=pool,
        config=config,
        rpc=rpc,
    )
    response = TerminateAgentResponse.model_validate(forwarded)
    if response.shell_sessions is not None and response.shell_sessions.killed:
        await _drop_killed_shell_ttls(pool, agent_id, response.shell_sessions.killed)
    try:
        hint = await asyncio.to_thread(_open_tasks_hint_blocking, pool, agent_id)
    except (psycopg.Error, PoolTimeout) as exc:
        _log.error(
            "[lifecycle] open-tasks hint for terminate of agent %s failed: %r",
            agent_id,
            exc,
        )
        return response
    return response.model_copy(update={"open_tasks": hint})


async def _drop_killed_shell_ttls(pool: ConnectionPool, agent_id: int, killed: list[int]) -> None:

    def _delete() -> None:
        with write_transaction(pool) as conn:
            conn.execute(
                "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = ANY(%s)",
                (agent_id, killed),
            )

    try:
        await asyncio.to_thread(_delete)
    except (psycopg.Error, PoolTimeout) as exc:
        _log.error(
            "[lifecycle] TTL rows of agent %s's killed shell sessions %s were not dropped "
            "(the TTL reaper retires them): %r",
            agent_id,
            killed,
            exc,
        )


def _open_tasks_hint_blocking(pool: ConnectionPool, agent_id: int) -> OpenTasksHint | None:

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, title, status, updated_at FROM agent_tasks "
            "WHERE owner = %s AND status = 'in_progress' "
            "ORDER BY updated_at DESC, id DESC",
            (agent_id,),
        ).fetchall()
    if not rows:
        return None
    shown = rows[:_OPEN_TASKS_SHOWN]
    return OpenTasksHint(
        count=len(rows),
        tasks=[
            OpenTaskRow(id=row[0], title=row[1], status=row[2], updated_at=row[3].isoformat())
            for row in shown
        ],
        more=len(rows) - len(shown),
    )


@router.post(
    "/api/agents/{agent_id}/resurrect",
    description="Resurrect a terminated agent — UPDATE 'terminated' -> 'idling' +\nlaunch a fresh detached process attached to the same agent_id\n(LangGraph state preserved; the agent wakes and continues from its last\nturn).\n\nUsed by the frontend resurrect button. A bare resurrect is a pure\nlifecycle event with no message — the agent just gets the \"you have been\nresurrected\" marker; the \"resurrect with prompt\" path carries a `prompt`\nthat is INSERTed as a chat inbound in the **same transaction** as the\nlifecycle 'resurrect' inbound. The detached session may be created before\ncommit, but its child blocks on the agent row and cannot claim or process\neither inbound early. `resurrected_by` defaults to\n\"user\" and is validated against the envelope source whitelist.\n\nPeer agents have no dedicated resurrect API — they `send_message`, and\nauto-resurrect (`deliver_chat_inbound`) wakes a terminated target. This\nendpoint covers the case auto-resurrect cannot: a resurrect with no\nmessage to deliver.\n\nAlways runs on the agent's home machine via its ops server\n(`forward_to_home_machine`) — that host starts the new process; launching\nanywhere else would start it on the wrong host.\n\n404: agent_id does not exist (AgentNotFound -> handler returns 404 + reason).\n`already_alive`: agent is still alive\n    (running/idling); resurrect does not\n    apply — idempotent.",
)
async def post_agent_resurrect(
    agent_id: int,
    request: Request,
    body: ResurrectAgentRequest = Body(default_factory=ResurrectAgentRequest),  # noqa: B008
) -> ResurrectAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/resurrect-explicit",
        body.model_dump(),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
        config=request.app.state.gateway_config,
        rpc=gateway_rpc(request),
    )
    return ResurrectAgentResponse.model_validate(forwarded)


def _resolve_restart_target_harness(
    overlay: Mapping[str, object] | None, current_harness: Harness
) -> Harness:
    if not overlay or "harness" not in overlay:
        return current_harness
    from base.api_contracts.agent_contract import LAUNCHABLE_HARNESSES

    raw_h = overlay["harness"]
    try:
        target_harness = coerce_harness(raw_h)
        is_launchable = target_harness.value in LAUNCHABLE_HARNESSES
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid harness in config_overlay: {exc}"
        ) from exc
    if not is_launchable:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid harness in config_overlay: Harness '{target_harness.value}' is not launchable",
        )
    return target_harness


def _verify_restart_preconditions(
    conn: psycopg.Connection[Any],
    agent_id: int,
    status: str,
    current_harness: Harness,
    target_harness: Harness,
) -> None:
    has_lease = (
        conn.execute(
            "SELECT 1 FROM agent_impersonations WHERE agent_id = %s "
            "AND (status IN ('requested', 'accepted', 'active') OR delta_version > applied_version OR handoff_applied_at IS NULL) LIMIT 1",
            (agent_id,),
        ).fetchone()
        is not None
    )
    if has_lease:
        raise HTTPException(status_code=409, detail="Agent has an active impersonation lease")

    if (
        target_harness != current_harness or current_harness is not Harness.MEIVA
    ) and status not in ("idling", "terminated"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot restart agent in status '{status}'; must be idling or terminated",
        )


def _validate_post_agent_restart_blocking(
    pool: ConnectionPool,
    agent_id: int,
    body: RestartAgentRequest,
    *,
    catalog: ModelCatalog,
    default_model: str,
    home: Path,
) -> None:
    overlay_without_harness = (
        {k: v for k, v in body.config_overlay.items() if k != "harness"}
        if body.config_overlay
        else {}
    )
    if overlay_without_harness:
        from base.packages.plugins.config_registration import validate_config_overlay
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        try:
            validate_config_overlay(
                overlay_without_harness,
                models=catalog.models,
                decision_models=catalog.decision_models,
                prices=catalog.prices,
                home=home,
            )
        except InvalidConfigOverlay as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    from base.agents.model.restart_config import validate_restart_model_config

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, harness, birth_config, config_overlay FROM agents_meta WHERE id=%s",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        status = str(row[0])
        current_harness = coerce_harness(row[1])
        target_harness = _resolve_restart_target_harness(body.config_overlay, current_harness)
        _verify_restart_preconditions(conn, agent_id, status, current_harness, target_harness)
        with conn.cursor() as cur:
            validate_restart_model_config(
                cur,
                agent_id,
                body.config_overlay or {},
                catalog=catalog,
                default_model=default_model,
                harness=target_harness,
            )


@router.post(
    "/api/agents/{agent_id}/restart",
    description="Enqueue restart or harness switch on the agent's home runner.\n\nRestart requires a non-empty message. For native meiva agents while alive,\nthe current turn reaches claim, returns normally and flushes its checkpoint.\nFor harness switching, external harnesses, or terminated agents, an exact\n4-step transition sequence settles, drops, updates harness, and resurrects.",
)
async def post_agent_restart(
    agent_id: int,
    request: Request,
    body: RestartAgentRequest,
) -> RestartAgentResponse:
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=422, detail="Restart requires a non-empty message")

    await asyncio.to_thread(
        _validate_post_agent_restart_blocking,
        request.app.state.db_pool,
        agent_id,
        body,
        catalog=require_model_catalog(request),
        default_model=gateway_config(request).models.llm_model,
        home=request.app.state.gateway_process.home,
    )

    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/restart",
        body.model_dump(),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
        config=request.app.state.gateway_config,
        rpc=gateway_rpc(request),
    )
    return RestartAgentResponse.model_validate(forwarded)


@router.post(
    "/api/agents/{agent_id}/cancel",
    description="Cancel the agent's running turn without terminating the agent.\n\nThe request is forwarded to the home runner, whose host cancels the running\nturn task. The agent stays alive and idles; its next input starts a fresh\nturn. `cancelled` means a running turn was cancelled; `not_running` means\nthere was none. An unreachable home runner is an error, not `not_running`.",
)
async def post_agent_cancel(agent_id: int, request: Request) -> CancelAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/cancel",
        {},
        db=request.app.state.db,
        pool=request.app.state.db_pool,
        config=request.app.state.gateway_config,
        rpc=gateway_rpc(request),
    )
    return CancelAgentResponse.model_validate(forwarded)


@router.post(
    "/api/agents/{agent_id}/compact",
    description="Ask the agent to compact its history at its next turn.\n\nInserts a `compact_request` inbound and wakes the host; the agent summarizes and\nreplaces its history when it next claims input. The response says the request\nis durable, not that the summary was applied. A terminated agent returns 409.",
)
async def post_agent_compact(agent_id: int, request: Request) -> CompactAgentResponse:

    return await asyncio.to_thread(
        _request_compaction_blocking,
        request.app.state.db_pool,
        request.app.state.db,
        request.app.state.bus,
        agent_id,
    )


def _request_compaction_blocking(
    pool: ConnectionPool, db: Database, bus: EventBus, agent_id: int
) -> CompactAgentResponse:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, harness FROM agents_meta WHERE id=%s", (agent_id,)
        ).fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        harness = coerce_harness(row[1])
        if harness is not Harness.MEIVA:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Compaction is not supported for external harness '{harness.value}'; "
                    f"the external CLI owns its own context window."
                ),
            )
        if row[0] == AgentStatus.TERMINATED:
            raise HTTPException(status_code=409, detail=f"agent {agent_id} is terminated")
        inbound_id = insert_compact_request_inbound(conn, agent_id, database=db, bus=bus)
    return CompactAgentResponse(inbound_id=inbound_id)
