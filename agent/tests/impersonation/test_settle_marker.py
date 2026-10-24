# meiva:note
# The clear side of the crash marker (#5468): a settle retry that succeeds may only erase the
# stamp that retry made. A NULL admission is the birth fact; a marker older than the admission
# belongs to a real turn crash, and clearing it would let a silent agent escape the reaper.

from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import clear_turn_fatal
from agent.ownership.hosted import stamp_turn_fatal
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation


def _marker(db_conn: psycopg.Connection, agent_id: int) -> object:
    row = db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id = %s", (agent_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def _set_marker(db_conn: psycopg.Connection, agent_id: int, *, minutes_ago: int | None) -> None:
    if minutes_ago is None:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = NULL WHERE id = %s", (agent_id,)
        )
    else:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - make_interval(mins => %s) "
            "WHERE id = %s",
            (minutes_ago, agent_id),
        )
    db_conn.commit()


async def test_clear_turn_fatal_only_clears_the_stamp_this_admission_made(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    db_conn.execute("UPDATE agents_meta SET last_admission_at = NULL WHERE id = %s", (agent_id,))
    db_conn.commit()
    _set_marker(db_conn, agent_id, minutes_ago=0)
    assert not await clear_turn_fatal(aops_pool, incarnation)
    assert _marker(db_conn, agent_id) is not None

    db_conn.execute(
        "UPDATE agents_meta SET last_admission_at = now() - interval '1 second' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    _set_marker(db_conn, agent_id, minutes_ago=30)
    assert not await clear_turn_fatal(aops_pool, incarnation)
    assert _marker(db_conn, agent_id) is not None
    assert not await clear_turn_fatal(
        aops_pool, RuntimeIncarnation(agent_id, uuid4(), incarnation.owner)
    )

    _set_marker(db_conn, agent_id, minutes_ago=None)
    assert (await stamp_turn_fatal(aops_pool, incarnation)).applied
    assert await clear_turn_fatal(aops_pool, incarnation)
    assert _marker(db_conn, agent_id) is None
