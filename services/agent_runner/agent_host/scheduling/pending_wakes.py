"""SQL for the hosted runner's pending wake scan and lane classification."""

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool


# meiva:note
# Scan new inputs and explicit controls; never revive an expired turn.
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
