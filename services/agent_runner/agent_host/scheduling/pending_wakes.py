"""SQL for the hosted runner's pending wake scan and lane classification."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.deploy.maintenance import admission


async def held_maintenance_wakes(pool: AsyncConnectionPool) -> list[int] | None:
    """The held cohort members whose restart command is still open in Postgres.

    A member whose command was ended after a failed continuation is not woken
    again; the journal alone cannot say so. None when no hold is active.
    """
    current = admission.snapshot()
    if current is None or current.maintenance is None:
        return None
    owed = {
        agent: command
        for agent in current.maintenance.commands
        if (command := admission.pending_command(agent)) is not None
    }
    if not owed:
        return []
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM inbound_messages WHERE id=ANY(%s) AND status IN ('pending','claimed')",
            (list(owed.values()),),
        )
        open_commands = {row[0] for row in await cursor.fetchall()}
    return [agent for agent, command in owed.items() if command in open_commands]


async def scan_rows(pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str) -> list[int]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                "SELECT m.id FROM agents_meta m WHERE m.machine=%s "
                "AND m.status IN ('idling','running') "
                "AND (EXISTS (SELECT 1 FROM inbound_messages pending "
                "WHERE pending.agent_id=m.id AND pending.status='pending') "
                "OR EXISTS (SELECT 1 FROM inbound_messages claimed "
                "WHERE claimed.agent_id=m.id AND claimed.kind IN ('restart','terminate') "
                "AND claimed.status='claimed') "
                "OR EXISTS (SELECT 1 FROM agent_impersonations lease "
                "WHERE lease.agent_id=m.id AND lease.status IN ('requested','accepted','active')))",
                (machine,),
            )
        ).fetchall()
    return [row[0] for row in rows]


async def scan_candidates(
    pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str
) -> list[int]:
    """Scan new inputs and explicit controls; never revive an expired turn.

    Held maintenance owns its restart cohort; otherwise retained commands share
    the work scan.
    """
    held = await held_maintenance_wakes(pool)
    if held is not None:
        return held
    return await scan_rows(pool, machine)
