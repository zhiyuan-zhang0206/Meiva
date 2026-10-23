# meiva:note
# Real PostgreSQL failures at the termination transaction's write boundaries.

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql


@contextmanager
def reject_pair_after_insert(conn: psycopg.Connection, agent_id: int) -> Generator[None]:
    conn.execute(
        "CREATE FUNCTION reject_termination_pair() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN IF EXISTS (SELECT 1 FROM inbound_messages WHERE agent_id=NEW.agent_id "
        "AND kind='chat') THEN RAISE EXCEPTION 'injected pair failure'; END IF; "
        "RETURN NEW; END $$"
    )
    conn.execute(
        sql.SQL(
            "CREATE TRIGGER reject_termination_pair AFTER INSERT ON inbound_messages "
            "FOR EACH ROW WHEN (NEW.agent_id={} AND NEW.kind='terminate') "
            "EXECUTE FUNCTION reject_termination_pair()"
        ).format(sql.Literal(agent_id))
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("DROP TRIGGER reject_termination_pair ON inbound_messages")
        conn.execute("DROP FUNCTION reject_termination_pair()")
        conn.commit()


@contextmanager
def reject_chat(conn: psycopg.Connection, agent_id: int) -> Generator[None]:
    conn.execute(
        sql.SQL(
            "ALTER TABLE inbound_messages ADD CONSTRAINT reject_termination_chat "
            "CHECK (agent_id != {} OR kind != 'chat')"
        ).format(sql.Literal(agent_id))
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("ALTER TABLE inbound_messages DROP CONSTRAINT reject_termination_chat")
        conn.commit()


@contextmanager
def reject_audit(conn: psycopg.Connection, agent_id: int) -> Generator[None]:
    conn.execute(
        sql.SQL(
            "ALTER TABLE audit_events ADD CONSTRAINT reject_termination_audit "
            "CHECK (agent_id != {} OR event_name != 'terminate')"
        ).format(sql.Literal(agent_id))
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("ALTER TABLE audit_events DROP CONSTRAINT reject_termination_audit")
        conn.commit()
