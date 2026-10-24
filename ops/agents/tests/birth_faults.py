# meiva:note
# Real PostgreSQL conditions at the writes of a birth transaction.
#
# `reject_audit_event` makes the database refuse an audit fact. `hold_chat_inserts` parks the
# next chat INSERT on a lock the test holds, so a test can look at the world while a birth is
# between its main transaction and its prompt.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql

__all__ = ["ChatInsertHold", "hold_chat_inserts", "reject_audit_event"]

_HOLD_KEY = 5_000_017


# meiva:note
# While the block runs, the database refuses to store an audit event named `event_name`.
#
# A CHECK constraint on `audit_events` makes the refusal the database's own, so the birth
# transaction meets the failure at its INSERT exactly as it would a full disk or a revoked grant.
@contextmanager
def reject_audit_event(conn: psycopg.Connection, event_name: str) -> Generator[None]:
    conn.execute(
        sql.SQL(
            "ALTER TABLE audit_events ADD CONSTRAINT reject_birth_audit CHECK (event_name != {})"
        ).format(sql.Literal(event_name))
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("ALTER TABLE audit_events DROP CONSTRAINT reject_birth_audit")
        conn.commit()


# meiva:note
# The held lock: `waiting` says a chat INSERT is parked on it, `release` lets that INSERT go on.
class ChatInsertHold:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        self._held = True

    def waiting(self) -> bool:
        row = self._conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)"
        ).fetchone()
        self._conn.commit()
        return row == (True,)

    def release(self) -> None:
        if self._held:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (_HOLD_KEY,))
            self._conn.commit()
            self._held = False


# meiva:note
# While the block runs, every INSERT of a chat inbound waits until `release` is called.
#
# A trigger takes the advisory lock `conn` holds, so the INSERT blocks inside its own
# transaction and nothing it would publish afterwards can happen early.
@contextmanager
def hold_chat_inserts(conn: psycopg.Connection) -> Generator[ChatInsertHold]:
    conn.execute(
        sql.SQL(
            "CREATE FUNCTION hold_chat_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM pg_advisory_xact_lock({}); RETURN NEW; END $$"
        ).format(sql.Literal(_HOLD_KEY))
    )
    conn.execute(
        "CREATE TRIGGER hold_chat_insert BEFORE INSERT ON inbound_messages "
        "FOR EACH ROW WHEN (NEW.kind = 'chat') EXECUTE FUNCTION hold_chat_insert()"
    )
    conn.execute("SELECT pg_advisory_lock(%s)", (_HOLD_KEY,))
    conn.commit()
    hold = ChatInsertHold(conn)
    try:
        yield hold
    finally:
        hold.release()
        conn.execute("DROP TRIGGER hold_chat_insert ON inbound_messages")
        conn.execute("DROP FUNCTION hold_chat_insert()")
        conn.commit()
