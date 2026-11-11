"""SQL for the hosted runner's pending wake scan and lane classification."""

from __future__ import annotations

import psycopg
from psycopg import sql as pgsql
from psycopg_pool import AsyncConnectionPool

from base.agents.messages.inbound import not_passive_inbound


# meiva:note
# Scan new inputs and explicit controls; never revive an expired turn, and never wake an idle
# agent for a passive lifecycle note. An idling agent on an external harness whose inbox still holds
# a row claimed for the harness is woken too: no invocation is running to settle it, so a crash or a
# restart left it handed over and unacknowledged, and the turn this wake starts reconciles it against
# the harness's record and delivers it again only when the record has no trace of it.
# meiva:link agent/graph/docs/harness-foreground.md
async def scan_rows(pool: AsyncConnectionPool[psycopg.AsyncConnection], machine: str) -> list[int]:
    async with pool.connection() as conn:
        rows = await (
            await conn.execute(
                pgsql.SQL(
                    "SELECT m.id FROM agents_meta m WHERE m.machine=%s "
                    "AND m.status IN ('idling','running') "
                    "AND (EXISTS (SELECT 1 FROM inbound_messages pending "
                    "WHERE pending.agent_id=m.id AND pending.status='pending' AND {not_passive}) "
                    "OR EXISTS (SELECT 1 FROM inbound_messages claimed "
                    "WHERE claimed.agent_id=m.id AND claimed.kind IN ('restart','terminate') "
                    "AND claimed.status='claimed') "
                    "OR (m.status='idling' AND m.harness <> 'meiva' AND EXISTS ("
                    "SELECT 1 FROM inbound_messages handed "
                    "WHERE handed.agent_id=m.id AND handed.status='claimed' "
                    "AND handed.kind IN ('chat','system_note','heartbeat') "
                    "AND COALESCE(handed.payload->>'harness_reconcile_deferred','')='')))"
                ).format(not_passive=pgsql.SQL(not_passive_inbound("pending"))),
                (machine,),
            )
        ).fetchall()
    return [row[0] for row in rows]
