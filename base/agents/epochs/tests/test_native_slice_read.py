from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.epochs.native_slice import NativeEpochSlice
from base.agents.epochs.read import read_native_slice
from base.db import create_agent

_SWITCHED = datetime(2026, 11, 25, 5, tzinfo=UTC)


def _chain(conn: psycopg.Connection, harnesses: list[str]) -> int:
    agent = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', %s)",
        (agent, harnesses[-1]),
    )
    for seq, harness in enumerate(harnesses):
        last = seq == len(harnesses) - 1
        conn.execute(
            "INSERT INTO agent_harness_epochs (agent_id, seq, harness, started_at, ended_at, "
            "switched_by) VALUES (%s, %s, %s, %s, %s, 'user')",
            (
                agent,
                seq,
                harness,
                datetime(2026, 11, 25, seq + 1, tzinfo=UTC),
                None if last else datetime(2026, 11, 25, seq + 2, tzinfo=UTC),
            ),
        )
    conn.commit()
    return agent


async def test_the_current_native_epoch_of_a_switched_agent_has_a_slice_from_its_start(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    agent = _chain(db_conn, ["meiva", "codex", "meiva"])
    epoch_slice = await read_native_slice(aops_pool, agent, "meiva")
    assert epoch_slice == NativeEpochSlice(
        not_before=datetime(2026, 11, 25, 3, tzinfo=UTC), before=None, first=False
    )


async def test_an_agent_with_one_native_epoch_or_an_external_harness_has_no_slice(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool
) -> None:
    never_switched = _chain(db_conn, ["meiva"])
    one_native = _chain(db_conn, ["meiva", "codex"])
    external = _chain(db_conn, ["meiva", "codex"])
    assert await read_native_slice(aops_pool, never_switched, "meiva") is None
    assert await read_native_slice(aops_pool, one_native, "codex") is None
    assert await read_native_slice(aops_pool, external, "codex") is None
