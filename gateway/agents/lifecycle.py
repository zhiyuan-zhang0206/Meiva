from __future__ import annotations

import asyncio
import logging
from typing import Literal

import psycopg
from fastapi import APIRouter, Body, HTTPException, Request
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, Field

from base.agents.impersonation import ImpersonationError
from base.agents.impersonation.maintenance import force_expire_impersonation
from base.agents.inbox.compact import insert_compact_request_inbound
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from gateway.agents.forward import forward_to_home_machine
from gateway.http.model_catalog import require_model_catalog
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
        agent_id, body, request.app.state.db_pool, db=request.app.state.db
    )


async def terminate_agent_with_open_tasks(
    agent_id: int, body: TerminateAgentRequest, pool: ConnectionPool, *, db: Database
) -> TerminateAgentResponse:

    forwarded = await forward_to_home_machine(
        agent_id, f"/api/agents/{agent_id}/terminate", body.model_dump(), db=db, pool=pool
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
    )
    return ResurrectAgentResponse.model_validate(forwarded)


@router.post(
    "/api/agents/{agent_id}/restart",
    description="Enqueue native restart on the agent's home runner.\n\nThe current turn reaches claim, returns normally and flushes its checkpoint.\nThe host then applies the exact command and releases the incarnation for\nnew admission, retaining agent ID and context. Terminated agents require\nresurrection; restart returns already_terminated for them.",
)
async def post_agent_restart(
    agent_id: int,
    request: Request,
    body: RestartAgentRequest = Body(default_factory=RestartAgentRequest),  # noqa: B008
) -> RestartAgentResponse:

    if body.config_overlay:
        from base.packages.plugins.config_registration import validate_config_overlay
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        catalog = require_model_catalog(request)
        try:
            validate_config_overlay(
                body.config_overlay,
                models=catalog.models,
                decision_models=catalog.decision_models,
            )
        except InvalidConfigOverlay as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    forwarded = await forward_to_home_machine(
        agent_id,
        f"/api/agents/{agent_id}/restart",
        body.model_dump(),
        db=request.app.state.db,
        pool=request.app.state.db_pool,
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
        row = conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        if row[0] == AgentStatus.TERMINATED:
            raise HTTPException(status_code=409, detail=f"agent {agent_id} is terminated")
        inbound_id = insert_compact_request_inbound(conn, agent_id, database=db, bus=bus)
    return CompactAgentResponse(inbound_id=inbound_id)
