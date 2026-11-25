# meiva:link base/agents/epochs/docs/epochs.md

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.epochs.chain import usable_epochs
from base.agents.epochs.model import HarnessEpoch
from base.agents.epochs.native_slice import NATIVE_HARNESS, NativeEpochSlice, native_slice_of
from base.db.rows import aquery_all, query_all

_SELECT = (
    "SELECT agent_id, seq, harness, model, session_id, started_at, ended_at, source_start, "
    "source_end, switched_by, reason FROM agent_harness_epochs WHERE agent_id = %s ORDER BY seq"
)


def read_epochs(conn: psycopg.Connection, agent_id: int) -> list[HarnessEpoch]:
    return query_all(conn, HarnessEpoch, _SELECT, (agent_id,))


async def aread_epochs(conn: psycopg.AsyncConnection, agent_id: int) -> list[HarnessEpoch]:
    return await aquery_all(conn, HarnessEpoch, _SELECT, (agent_id,))


async def read_native_slice(
    pool: AsyncConnectionPool, agent_id: int, harness: str
) -> NativeEpochSlice | None:
    async with pool.connection() as conn:
        epochs = usable_epochs(await aread_epochs(conn, agent_id), harness)
    return native_slice_of(epochs, epochs[-1].seq) if epochs and harness == NATIVE_HARNESS else None
