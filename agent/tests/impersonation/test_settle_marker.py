# meiva:note
# The clear side of the crash marker (#5468): a settle that writes the row's receipt may only
# erase the marker while nothing of that row's work is left owing. The condition is the fact, not
# the marker's timestamp — a COALESCE stamp keeps the FIRST death, so an earlier wake's stamp is
# always older than a later admission, and a timestamp test would strand it on a row this wake
# settled (the reaper would then terminate a healthy agent fifteen minutes later).

from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.impersonation import clear_turn_fatal
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base.db.code_version_gate import ProcessDbGate
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
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


def _lease(db_conn: psycopg.Connection, agent_id: int, *, receipt: bool) -> None:
    db_conn.execute(
        "INSERT INTO agent_impersonations"
        "(id,agent_id,session_id,source,machine,status,ttl_seconds,expires_at,ended_at,"
        "handoff_applied_at) VALUES (gen_random_uuid(),%s,0,'external_agent:codex:test',"
        "'claim-test','released',300,clock_timestamp(),clock_timestamp(),"
        "CASE WHEN %s THEN clock_timestamp() END)",
        (agent_id, receipt),
    )
    db_conn.commit()


# meiva:note
# A marker left behind is the dangerous direction here, so the refused cases are the ones this
# asserts first: an ended lease that still owes its receipt keeps the marker (the wake has not
# finished that row's work), and a marker written by another runtime is never ours to clear.
async def test_clear_turn_fatal_requires_that_no_receipt_is_still_owed(
    loguru_records: LogRecords,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    _set_marker(db_conn, agent_id, minutes_ago=30)
    _lease(db_conn, agent_id, receipt=False)
    assert not await clear_turn_fatal(aops_pool, incarnation)
    assert _marker(db_conn, agent_id) is not None

    assert not await clear_turn_fatal(
        aops_pool, RuntimeIncarnation(agent_id, uuid4(), incarnation.owner)
    )
    assert _marker(db_conn, agent_id) is not None

    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at = clock_timestamp() "
        "WHERE agent_id = %s",
        (agent_id,),
    )
    db_conn.commit()
    assert await clear_turn_fatal(aops_pool, incarnation)
    assert _marker(db_conn, agent_id) is None
    assert not await clear_turn_fatal(aops_pool, incarnation)
    assert loguru_records.events("hosted_handoff_marker_cleared_while_ineligible") == []

    # meiva:note
    # The narrowed case this change introduces: the marker is gone and the heartbeat will not
    # pick the row up (a permanent reject streak keeps it out of every automatic path). Nothing
    # is wrong with the clear; the log has to say that this row's only recovery is a manual one.
    _set_marker(db_conn, agent_id, minutes_ago=5)
    db_conn.execute("UPDATE agents_meta SET permanent_reject_streak = 3 WHERE id = %s", (agent_id,))
    db_conn.commit()
    assert await clear_turn_fatal(aops_pool, incarnation)
    [event] = loguru_records.events("hosted_handoff_marker_cleared_while_ineligible")
    assert event["extra"]["permanent_reject_streak"] == 3
    assert event["extra"]["heartbeat_paused"] is False
