from __future__ import annotations

import psycopg

from base.agents.history.harness_messages import copy_harness_timeline


def _seed_agent(conn: psycopg.Connection) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', 'claude_code')",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def test_a_forked_timeline_is_copied_with_its_sequence_and_without_its_inbound_ids(
    db_conn: psycopg.Connection,
) -> None:
    source, clone = _seed_agent(db_conn), _seed_agent(db_conn)
    for seq in range(2):
        db_conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item) "
            "VALUES (%s, %s, 'claude_code', %s::jsonb)",
            (source, seq, f'{{"item_id": "h{seq}.0"}}'),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        assert copy_harness_timeline(cur, source, clone) == 2
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT seq, inbound_id FROM agent_harness_messages WHERE agent_id = %s ORDER BY seq",
        (clone,),
    ).fetchall()
    assert rows == [(0, None), (1, None)]
