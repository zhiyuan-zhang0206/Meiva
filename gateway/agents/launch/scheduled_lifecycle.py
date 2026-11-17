# meiva:note
# One-shot lifecycle appointments and initiator-routed coordination notices.
# Existing lifecycle paths own mutation and fencing; appointments never poll or lock inboxes.

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Request

from base.daemon.schedules.watcher import normalize_when, parse_timeout
from base.db.rows import query_one
from gateway.agents.state import post_agent_message
from gateway.http.config import GatewayAppState, gateway_clock, gateway_state, state_as
from ops.rpc_schemas import AgentMessageIn

Action = Literal["transfer", "restart"]


CreateAppointment = Callable[[Request, str, datetime, str, str], Awaitable[int]]


class AppointmentAppState(GatewayAppState):
    lifecycle_schedule_creator: CreateAppointment


@dataclass(frozen=True)
class AppointmentTarget:
    status: str


def target_status_blocking(request: Request, agent_id: int) -> str:
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, AppointmentTarget, "SELECT status FROM agents_meta WHERE id = %s", (agent_id,)
        )
    if row is None:
        raise HTTPException(404, f"agent {agent_id} does not exist")
    return row.status


@dataclass(frozen=True)
class AppointmentMachine:
    ready: bool


def validate_appointment_machine_blocking(request: Request, agent_id: int) -> None:
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn,
            AppointmentMachine,
            "SELECT (m.paused_at IS NULL AND m.stopped_at IS NULL "
            "AND 'agent-runner' = ANY(m.role) AND m.gateway_url IS NOT NULL) AS ready "
            "FROM agents_meta a JOIN machines m ON m.name = a.machine WHERE a.id = %s",
            (agent_id,),
        )
    if row is None or not row.ready:
        raise HTTPException(409, "agent's machine must be an active, reachable agent-runner")


def parse_at(spec: str, *, now: datetime) -> datetime:
    when = timedelta(seconds=parse_timeout(spec[1:])) if spec.startswith("+") else spec
    run_at = normalize_when(when, now=now)
    if run_at <= now:
        raise ValueError("--at must name a future time")
    return run_at


def outcome_recipients(
    agent_id: int, source: str, *, failed: bool, running: bool
) -> tuple[int, ...]:
    caller = int(source[6:]) if source.startswith("agent:") else None
    if caller == agent_id:
        return (agent_id,) if failed else ()
    recipients = (caller,) if caller is not None else ()
    return recipients + ((agent_id,) if failed and running else ())


async def notify(request: Request, agent_id: int, content: str, source: str) -> None:
    await post_agent_message(
        agent_id, AgentMessageIn(content=content, source=source), request, idempotency_key=None
    )


async def arm_appointment(
    request: Request,
    agent_id: int,
    *,
    action: Action,
    at: str,
    source: str,
    message: str | None,
    command: list[str],
) -> dict[str, object]:
    try:
        run_at = parse_at(at, now=gateway_clock(request).now())
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    status = await asyncio.to_thread(target_status_blocking, request, agent_id)
    # meiva:note
    # A schedule owns its execution independently of the initiating agent's lifetime.
    script = "import subprocess\nsubprocess.run(" + repr(command) + ", check=True)\n"
    schedule_id = await state_as(request, AppointmentAppState).lifecycle_schedule_creator(
        request,
        f"{action}-{agent_id}-{run_at.isoformat()}",
        run_at,
        script,
        f"{source}: {action} agent {agent_id}",
    )
    if source != f"agent:{agent_id}" and status == "running":
        await notify(
            request,
            agent_id,
            f"Scheduled {action} #{schedule_id} at {run_at.isoformat()} by {source}. "
            f"Finish your current turn before then. {message or ''}",
            source,
        )
    return {"schedule_id": schedule_id, "run_at": run_at.isoformat()}


async def single_attempt(
    request: Request,
    agent_id: int,
    *,
    action: Action,
    source: str,
    execute: Callable[[], Awaitable[object]],
) -> object:
    status = await asyncio.to_thread(target_status_blocking, request, agent_id)
    # meiva:note
    # A scheduled native restart must also preserve a still-running turn.
    try:
        if status not in ("idling", "terminated"):
            raise HTTPException(409, f"Scheduled {action} refused: agent {agent_id} is {status}")  # noqa: TRY301
        result = await execute()
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        status = await asyncio.to_thread(target_status_blocking, request, agent_id)
        for recipient in outcome_recipients(
            agent_id, source, failed=True, running=status == "running"
        ):
            await notify(
                request,
                recipient,
                f"Scheduled {action} of agent {agent_id} failed: {detail}",
                source,
            )
        raise
    for recipient in outcome_recipients(agent_id, source, failed=False, running=False):
        await notify(
            request, recipient, f"Scheduled {action} of agent {agent_id} completed.", source
        )
    return result
