from __future__ import annotations

import psycopg
import pytest
from psycopg import errors


def _insert_identity(db_conn: psycopg.Connection, agent_id: int) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s)", (agent_id,))
    db_conn.commit()


def _insert_meta(db_conn: psycopg.Connection, agent_id: int, harness: str | None = None) -> None:
    with db_conn.cursor() as cur:
        if harness is None:
            cur.execute("INSERT INTO agents_meta (id, status) VALUES (%s, 'idling')", (agent_id,))
        else:
            cur.execute(
                "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'idling', %s)",
                (agent_id, harness),
            )
    db_conn.commit()


def _birth(db_conn: psycopg.Connection, agent_id: int, harness: str | None = None) -> None:
    _insert_identity(db_conn, agent_id)
    _insert_meta(db_conn, agent_id, harness)


def _harness_of(db_conn: psycopg.Connection, agent_id: int) -> str:
    with db_conn.cursor() as cur:
        cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _row_count(db_conn: psycopg.Connection, agent_id: int) -> int:
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_birth_without_an_explicit_harness_is_meiva(db_conn: psycopg.Connection) -> None:
    _birth(db_conn, 501)
    assert _harness_of(db_conn, 501) == "meiva"


@pytest.mark.parametrize("harness", ["meiva", "claude_code", "codex", "antigravity"])
def test_every_declared_harness_is_accepted(db_conn: psycopg.Connection, harness: str) -> None:
    _birth(db_conn, 502, harness)
    assert _harness_of(db_conn, 502) == harness


def test_unknown_harness_is_rejected_by_the_column(db_conn: psycopg.Connection) -> None:
    _insert_identity(db_conn, 503)

    with pytest.raises(errors.CheckViolation), db_conn.transaction():
        db_conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (503, 'idling', 'claude-code')"
        )

    assert _row_count(db_conn, 503) == 0


def test_changing_the_harness_after_birth_is_rejected(db_conn: psycopg.Connection) -> None:
    _birth(db_conn, 504, "codex")

    with (
        pytest.raises(errors.RaiseException, match="harness is immutable"),
        db_conn.transaction(),
    ):
        db_conn.execute("UPDATE agents_meta SET harness = 'meiva' WHERE id = 504")

    assert _harness_of(db_conn, 504) == "codex"


def test_upserting_a_changed_harness_is_rejected(db_conn: psycopg.Connection) -> None:
    _birth(db_conn, 506, "codex")

    with (
        pytest.raises(errors.RaiseException, match="harness is immutable"),
        db_conn.transaction(),
    ):
        db_conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (506, 'idling', 'meiva') "
            "ON CONFLICT (id) DO UPDATE SET harness = EXCLUDED.harness"
        )

    assert _harness_of(db_conn, 506) == "codex"


def test_other_updates_leave_the_harness_alone(db_conn: psycopg.Connection) -> None:
    _birth(db_conn, 505, "antigravity")

    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 505")
        cur.execute("UPDATE agents_meta SET status = 'idling' WHERE id = 505")
        cur.execute("UPDATE agents_meta SET harness = harness WHERE id = 505")
    db_conn.commit()

    assert _harness_of(db_conn, 505) == "antigravity"
