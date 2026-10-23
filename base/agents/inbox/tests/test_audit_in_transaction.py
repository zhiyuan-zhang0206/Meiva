from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base.agents.inbox.compact import insert_compact_request_inbound
from base.agents.inbox.insert import insert_inbound_message, insert_spawn_prompt_in_transaction
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.telemetry import Event


def _audit(
    conn: psycopg.Connection, agent_id: int, event_name: str
) -> list[tuple[str, int | None, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT source, target_agent_id, attributes FROM audit_events "
        "WHERE agent_id=%s AND event_name=%s ORDER BY id",
        (agent_id, event_name),
    ).fetchall()
    conn.commit()
    return [(r[0], r[1], r[2]) for r in rows]


def test_an_inbound_and_its_audit_fact_commit_together(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    agent_id = create_agent(db_conn)

    inbound_id = insert_inbound_message(
        db_conn, agent_id, "", source="user", kind="restart", bus=event_bus, database=database
    )

    assert _audit(db_conn, agent_id, "restart") == [("user", None, {"inbound_id": inbound_id})]


def test_an_inbound_whose_audit_fact_cannot_be_recorded_is_not_written(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "CREATE FUNCTION pg_temp.reject_restart_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION 'audit write failed' USING ERRCODE = '23514'; END $$"
    )
    db_conn.execute(
        "CREATE TRIGGER reject_restart_audit BEFORE INSERT ON audit_events "
        "FOR EACH ROW WHEN (NEW.event_name = 'restart') "
        "EXECUTE FUNCTION pg_temp.reject_restart_audit()"
    )
    db_conn.commit()
    try:
        with pytest.raises(psycopg.errors.CheckViolation, match="audit write failed"):
            insert_inbound_message(
                db_conn,
                agent_id,
                "",
                source="user",
                kind="restart",
                bus=event_bus,
                database=database,
            )
        db_conn.rollback()
    finally:
        db_conn.rollback()
        db_conn.execute("DROP TRIGGER reject_restart_audit ON audit_events")
        db_conn.execute("DROP FUNCTION pg_temp.reject_restart_audit()")
        db_conn.commit()

    count = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone()
    assert count == (0,)
    assert _audit(db_conn, agent_id, "restart") == []


def test_a_compact_request_is_recorded_before_it_is_emitted(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    agent_id = create_agent(db_conn)
    seen_at_emit: list[int] = []

    def emit(_event: Event | None) -> None:
        seen_at_emit.append(len(_audit(db_conn, agent_id, "compact")))

    insert_compact_request_inbound(
        db_conn, agent_id, bus=event_bus, database=database, emit_event=emit
    )

    assert seen_at_emit == [1]
    assert _audit(db_conn, agent_id, "compact") == [("user", None, {"compact_kind": "request"})]


def test_an_agent_sourced_spawn_prompt_is_recorded_with_its_inbound(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = create_agent(db_conn)

    with db_conn.cursor() as cur:
        inbound_id, event = insert_spawn_prompt_in_transaction(cur, agent_id, "hi", "agent:4242")
    db_conn.commit()

    assert event is not None
    assert _audit(db_conn, agent_id, "send_message") == [
        ("agent:4242", 4242, {"inbound_id": inbound_id, "content": "hi"})
    ]


def test_a_user_sourced_spawn_prompt_has_no_audit_fact(db_conn: psycopg.Connection) -> None:
    agent_id = create_agent(db_conn)

    with db_conn.cursor() as cur:
        _inbound_id, event = insert_spawn_prompt_in_transaction(cur, agent_id, "hi", "user")
    db_conn.commit()

    assert event is None
    assert _audit(db_conn, agent_id, "send_message") == []
