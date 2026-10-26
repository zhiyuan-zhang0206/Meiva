from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import (
    admit_hosted_runtime,
    settle_and_stamp_turn,
)
from base.clock.tests.seeded_rng import seeded_rng
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus


def _agent(conn: psycopg.Connection[Any], machine: str = "host-test") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine) VALUES (%s, 'idling', %s) "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = %s",
        (agent_id, machine, machine),
    )
    conn.commit()
    return agent_id


async def test_corpse_stamp_failure_keeps_its_traceback(
    redis_url: str,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    loguru_records: list[dict[str, Any]],
    database_gate: ProcessDbGate,
) -> None:
    agent_id, owner = _agent(db_conn), uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        "host-test",
        owner,
        db=Database.from_settings(gate=database_gate),
    )
    assert incarnation is not None

    db_conn.execute(
        "CREATE FUNCTION fault_corpse_stamp() RETURNS trigger LANGUAGE plpgsql AS "
        "$body$ BEGIN IF NEW.last_turn_fatal_at IS NOT NULL THEN "
        "RAISE EXCEPTION 'stamp store down'; END IF; RETURN NEW; END $body$"
    )
    db_conn.execute(
        "CREATE TRIGGER fault_corpse_stamp_trigger BEFORE UPDATE ON agents_meta "
        "FOR EACH ROW EXECUTE FUNCTION fault_corpse_stamp()"
    )
    db_conn.commit()
    try:
        settlement = await settle_and_stamp_turn(
            aops_pool,
            incarnation,
            bus=EventBus.from_settings(rng=seeded_rng()),
            exited=False,
            crashed=True,
        )
    finally:
        db_conn.rollback()
        db_conn.execute("DROP TRIGGER fault_corpse_stamp_trigger ON agents_meta")
        db_conn.execute("DROP FUNCTION fault_corpse_stamp()")
        db_conn.commit()

    assert settlement.stamp is None
    assert settlement.settled is True
    record = next(r for r in loguru_records if r["extra"].get("event") == "corpse_stamp_failed")
    assert record["exception"] is not None
    assert record["exception"].type is psycopg.errors.RaiseException
