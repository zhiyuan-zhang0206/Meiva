from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.db import create_agent
from services.agent_runner.agent_host.scheduling.pending_wakes import scan_rows


@pytest.mark.parametrize(
    ("status", "receipt", "deltas", "expected"),
    [
        ("requested", False, False, True),
        ("expired", False, False, True),
        ("rejected", False, False, True),
        ("released", True, True, True),
        ("released", True, False, False),
    ],
)
async def test_cold_host_finishes_takeover_without_an_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    receipt: bool,
    deltas: bool,
    expected: bool,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','handoff-scan')", (agent_id,)
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,expires_at,"
        "handoff_applied_at,plugin_delta,delta_version) "
        "VALUES(%s,%s,'external_agent:codex','handoff-scan',%s,300,clock_timestamp(),"
        "CASE WHEN %s THEN clock_timestamp() END,CASE WHEN %s THEN '[{}]'::jsonb ELSE '[]'::jsonb END,%s)",
        (uuid4(), agent_id, status, receipt, deltas, int(deltas)),
    )
    db_conn.commit()
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert (agent_id in await scan_rows(aops_pool, "handoff-scan")) is expected
