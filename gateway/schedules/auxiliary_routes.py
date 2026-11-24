# meiva:note
# Schedule drafting and machine-authenticated launch-environment routes.

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg_pool import ConnectionPool

from base.db.rows import query_one
from gateway.agents.creation import (
    DRAFT_AGENT_CONFIG,
    create_and_launch_agent,
    guarded_draft_key,
)
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.config import (
    gateway_clock,
    gateway_ensure_harnesses,
    gateway_rpc,
    gateway_state,
)
from gateway.http.model_catalog import require_model_catalog
from gateway.schedules import plans
from gateway.schedules.contracts import ScheduleDraftRequest, ScheduleDraftResponse
from gateway.schedules.rows import LaunchEnvRow
from ops.rpc_schemas import SpawnAgentRequest

router = APIRouter()


@router.post("/api/keyed/v1/schedules/draft")
async def guarded_draft_schedule(
    body: ScheduleDraftRequest, request: Request, key: str = Depends(guarded_draft_key)
) -> ScheduleDraftResponse:
    prompt = (
        "You are a schedule writer. Read and follow meiva.skills.meiva_guide.schedules to turn "
        "this request into a declarative schedule: clarify its trigger and payload, then create "
        "it via POST /api/schedules. Request:\n\n" + body.nl
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="meiva-schedule-writer",
        config=dict(DRAFT_AGENT_CONFIG),
    )
    state = gateway_state(request)
    spawned = await create_and_launch_agent(
        body_obj,
        state.machine.name(),
        state.db_pool,
        state.db,
        state.bus,
        catalog=require_model_catalog(request),
        authority=state.config_authority,
        creation_key=key,
        clock=gateway_clock(request),
        rpc=gateway_rpc(request),
        creation_identity=body.model_dump(mode="json"),
        ensure_harnesses=gateway_ensure_harnesses(request),
        environ=state.gateway_process.config.process_environment(),
    )
    return ScheduleDraftResponse(agent_id=spawned.id)


def _launch_env_blocking(pool: ConnectionPool, schedule_id: int) -> tuple[bool, tuple[str, ...]]:
    with pool.connection() as conn:
        row = query_one(
            conn,
            LaunchEnvRow,
            "SELECT enabled, secret_env_vars FROM schedules WHERE id = %s",
            (schedule_id,),
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row.enabled, tuple(row.secret_env_vars)


async def _read_gateway_secret_sources(
    request: Request,
) -> tuple[dict[str, str], dict[str, str]]:
    state = gateway_state(request)
    try:
        aliases = await asyncio.to_thread(state.config_authority.read_env_aliases)
    except OSError:
        aliases = {}
    process_env = state.gateway_process.config.process_environment()
    return dict(aliases), dict(process_env)


@router.get("/api/schedules/{schedule_id}/launch-env")
async def get_schedule_launch_env(
    request: Request,
    schedule_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    state = gateway_state(request)
    if (
        cluster_credential(
            authorization,
            state.gateway_config.cluster_secret,
            home=state.gateway_process.home,
            cache=state.machine_token_acceptance,
        )
        is None
    ):
        raise HTTPException(status_code=401, detail="machine API token required")

    enabled, secret_env_vars = await asyncio.to_thread(
        _launch_env_blocking, state.db_pool, schedule_id
    )
    if not enabled or not secret_env_vars:
        return {}
    aliases, process_env = await _read_gateway_secret_sources(request)
    return plans.select_declared_secrets(secret_env_vars, aliases, process_env)
