# meiva:link base/agents/epochs/docs/epochs.md

from __future__ import annotations

import psycopg

from base.agents.epochs.model import HarnessEpoch
from base.db.rows import aquery_all, query_all

_SELECT = (
    "SELECT agent_id, seq, harness, model, session_id, started_at, ended_at, source_start, "
    "source_end, switched_by, reason FROM agent_harness_epochs WHERE agent_id = %s ORDER BY seq"
)


def read_epochs(conn: psycopg.Connection, agent_id: int) -> list[HarnessEpoch]:
    return query_all(conn, HarnessEpoch, _SELECT, (agent_id,))


async def aread_epochs(conn: psycopg.AsyncConnection, agent_id: int) -> list[HarnessEpoch]:
    return await aquery_all(conn, HarnessEpoch, _SELECT, (agent_id,))
