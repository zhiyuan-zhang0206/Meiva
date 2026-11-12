from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from services.upkeep.events_maintenance.transcript_retention import sweep_expired_transcript_rows


def _seed(conn: psycopg.Connection, ages_days: list[int]) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    now = datetime.now(UTC)
    for ordinal, age in enumerate(ages_days):
        conn.execute(
            "INSERT INTO agent_harness_transcript_rows "
            "(agent_id, session_id, record, ordinal, row, written_at) "
            "VALUES (%s, 's', 'main', %s, '{}'::jsonb, %s)",
            (agent_id, ordinal, now - timedelta(days=age)),
        )
    conn.commit()
    return agent_id


def _left(conn: psycopg.Connection, agent_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT ordinal FROM agent_harness_transcript_rows WHERE agent_id = %s ORDER BY ordinal",
        (agent_id,),
    ).fetchall()
    return [int(row[0]) for row in rows]


def test_a_pass_deletes_only_expired_rows_in_bounded_batches(db_conn: psycopg.Connection) -> None:
    agent_id = _seed(db_conn, [40, 35, 31, 29, 1, 0])
    now = datetime.now(UTC)
    first = sweep_expired_transcript_rows(db_conn, now=now, batch_rows=2, max_batches=1)
    assert first == 2 and _left(db_conn, agent_id) == [2, 3, 4, 5]
    rest = sweep_expired_transcript_rows(db_conn, now=now, batch_rows=2, max_batches=5)
    assert rest == 1 and _left(db_conn, agent_id) == [3, 4, 5]
    assert sweep_expired_transcript_rows(db_conn, now=now) == 0


def test_the_retention_is_configurable_per_call(db_conn: psycopg.Connection) -> None:
    agent_id = _seed(db_conn, [10, 5, 1])
    now = datetime.now(UTC)
    assert sweep_expired_transcript_rows(db_conn, now=now, retention_days=3) == 2
    assert _left(db_conn, agent_id) == [2]
