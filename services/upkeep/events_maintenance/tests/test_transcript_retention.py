from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from services.upkeep.events_maintenance.transcript_retention import sweep_expired_transcript_rows

MIGRATION = next(Path(__file__).parents[4].glob("migrations/**/*harness-timeline-source-key.sql"))


def _seed(
    conn: psycopg.Connection,
    ages_days: list[int],
    *,
    rebuild_ordinal: int | None = None,
    record: str = "main",
) -> int:
    row = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    now = datetime.now(UTC)
    for ordinal, age in enumerate(ages_days):
        conn.execute(
            "INSERT INTO agent_harness_transcript_rows "
            "(agent_id, session_id, record, ordinal, row, written_at) "
            "VALUES (%s, 's', %s, %s, '{}'::jsonb, %s)",
            (agent_id, record, ordinal, now - timedelta(days=age)),
        )
    if rebuild_ordinal is not None:
        conn.execute(
            "INSERT INTO agent_harness_transcript_positions "
            "(agent_id, session_id, record, position, rebuild_ordinal) VALUES (%s, 's', %s, '{}', %s)",
            (agent_id, record, rebuild_ordinal),
        )
    conn.commit()
    return agent_id


def _left(conn: psycopg.Connection, agent_id: int, record: str = "main") -> list[int]:
    rows = conn.execute(
        "SELECT ordinal FROM agent_harness_transcript_rows "
        "WHERE agent_id = %s AND record = %s ORDER BY ordinal",
        (agent_id, record),
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


# meiva:note
# An idle live session: everything is older than the retention, the rebuild point is row 3.
def test_rows_after_the_rebuild_point_are_kept_however_old(db_conn: psycopg.Connection) -> None:
    agent_id = _seed(db_conn, [90, 80, 70, 60, 50, 45], rebuild_ordinal=3)
    now = datetime.now(UTC)
    assert sweep_expired_transcript_rows(db_conn, now=now) == 4
    assert _left(db_conn, agent_id) == [4, 5]
    assert sweep_expired_transcript_rows(db_conn, now=now) == 0


def test_a_record_with_no_quiet_point_yet_loses_nothing_and_one_without_a_position_expires(
    db_conn: psycopg.Connection,
) -> None:
    never_quiet = _seed(db_conn, [90, 80], rebuild_ordinal=-1)
    orphan = _seed(db_conn, [90, 80])
    assert sweep_expired_transcript_rows(db_conn, now=datetime.now(UTC)) == 2
    assert _left(db_conn, never_quiet) == [0, 1] and _left(db_conn, orphan) == []


# meiva:note
# The oldest rows are protected (after the rebuild point of an idle record); a batch still reaches the
# expired rows of the other record that are newer than them.
def test_protected_old_rows_do_not_hold_back_the_expiry_behind_them(
    db_conn: psycopg.Connection,
) -> None:
    idle = _seed(db_conn, [100, 99], rebuild_ordinal=-1)
    busy = _seed(db_conn, [50, 49, 48, 1], rebuild_ordinal=2)
    assert sweep_expired_transcript_rows(db_conn, now=datetime.now(UTC), batch_rows=2) == 3
    assert _left(db_conn, idle) == [0, 1] and _left(db_conn, busy) == [3]


@pytest.fixture
def gateway_login(db_conn: psycopg.Connection) -> Iterator[str]:
    """A role holding exactly the grants the migration gives `meiva_gateway`, on the real table."""

    role = "t_transcript_retention_gateway"
    grants = re.findall(
        r"GRANT [^;]*? ON agent_harness_transcript_\w+ TO meiva_gateway;", MIGRATION.read_text()
    )
    assert len(grants) == 2
    name = sql.Identifier(role)
    db_conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(name))
    db_conn.execute(sql.SQL("CREATE ROLE {}").format(name))
    for grant in grants:
        db_conn.execute(sql.SQL(grant.replace("meiva_gateway", "{}")).format(name))
    db_conn.commit()
    try:
        yield role
    finally:
        db_conn.rollback()
        for table in ("agent_harness_transcript_rows", "agent_harness_transcript_positions"):
            db_conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(table), name))
        db_conn.execute(sql.SQL("DROP ROLE {}").format(name))
        db_conn.commit()


def test_the_gateway_login_that_runs_events_maintenance_can_run_the_sweep(
    db_conn: psycopg.Connection, gateway_login: str
) -> None:
    agent_id = _seed(db_conn, [90, 80, 1], rebuild_ordinal=1)
    db_conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(gateway_login)))
    try:
        assert sweep_expired_transcript_rows(db_conn, now=datetime.now(UTC)) == 2
    finally:
        db_conn.execute("RESET ROLE")
        db_conn.commit()
    assert _left(db_conn, agent_id) == [2]
