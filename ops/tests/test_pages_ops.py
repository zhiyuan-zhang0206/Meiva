from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from base.db import create_agent
from ops.pages import PageRow, register_page_in_transaction

_HOST = "127.0.0.1"
_NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _register(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    host: str,
    title: str | None,
    *,
    serve_dir: str | None = None,
    ttl_seconds: int = 300,
    now: datetime = _NOW,
) -> PageRow:
    row = register_page_in_transaction(
        conn, agent_id, name, port, host, title, serve_dir, ttl_seconds=ttl_seconds, now=now
    )
    conn.commit()
    return row


def _page_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, port, host, title, serve_dir, closed_at FROM agent_pages "
            "WHERE agent_id = %s ORDER BY id ASC",
            (agent_id,),
        )
        return cur.fetchall()


def _page_deadline(
    conn: psycopg.Connection, agent_id: int, name: str
) -> tuple[datetime | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expires_at, expired_at FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


# meiva:note
# A fresh page expires `ttl_seconds` after the `now` the caller read, not after any clock the
# registration reads for itself.
def test_register_page_expires_ttl_seconds_after_the_callers_now(
    db_conn: psycopg.Connection,
) -> None:
    aid = create_agent(db_conn)

    _register(db_conn, aid, "fresh", 8000, _HOST, None, ttl_seconds=90)

    assert _page_deadline(db_conn, aid, "fresh") == (_NOW + timedelta(seconds=90), None)


# meiva:note
# Re-registering an expired page revives the same row with a new port, title and a deadline
# measured from the new `now`.
def test_register_page_revives_expired_row_and_resets_deadline(
    db_conn: psycopg.Connection,
) -> None:
    aid = create_agent(db_conn)
    original = _register(db_conn, aid, "revive", 8001, _HOST, None, ttl_seconds=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE id = %s",
            (original.id,),
        )
    db_conn.commit()

    later = _NOW + timedelta(hours=1)
    revived = _register(db_conn, aid, "revive", 8002, _HOST, "Again", ttl_seconds=240, now=later)

    assert revived.id == original.id
    assert revived.port == 8002
    assert _page_deadline(db_conn, aid, "revive") == (later + timedelta(seconds=240), None)


def test_register_page_upsert_updates_serve_dir(db_conn: psycopg.Connection) -> None:
    aid = create_agent(db_conn)
    r1 = _register(db_conn, aid, "p", 8001, _HOST, None, serve_dir="/data/a")
    r2 = _register(db_conn, aid, "p", 8002, "10.0.0.2", "v2", serve_dir="/data/b")
    assert r2.id == r1.id
    assert r2.port == 8002
    assert r2.title == "v2"
    assert r2.serve_dir == "/data/b"
    db_conn.rollback()
    rows = _page_rows(db_conn, aid)
    assert rows == [("p", 8002, "10.0.0.2", "v2", "/data/b", None)]


def test_register_page_raced_conflict_raises_domain_error(
    db_conn: psycopg.Connection,
) -> None:
    from ops.pages import PagePortConflictError

    owner = create_agent(db_conn)
    other = create_agent(db_conn)
    _register(db_conn, owner, "holder", 8776, _HOST, None)
    with pytest.raises(PagePortConflictError, match="'holder'"):
        _register(db_conn, other, "clash", 8776, _HOST, None)


@pytest.mark.parametrize("changed", [False, True])
def test_explicit_registration_resets_admission_only_for_a_changed_server(
    db_conn: psycopg.Connection, changed: bool
) -> None:
    aid = create_agent(db_conn)
    original = _register(db_conn, aid, "admission", 8900, _HOST, None, serve_dir="/data/a")
    db_conn.execute(
        "UPDATE agent_pages SET session_name = 'known-page-session', server_token = 'token' "
        "WHERE id = %s",
        (original.id,),
    )
    db_conn.commit()
    _register(
        db_conn,
        aid,
        "admission",
        8901 if changed else 8900,
        _HOST,
        "updated title",
        serve_dir="/data/a",
    )
    state = db_conn.execute(
        "SELECT session_name, server_token FROM agent_pages WHERE id = %s", (original.id,)
    ).fetchone()
    assert state == ((None, None) if changed else ("known-page-session", "token"))
