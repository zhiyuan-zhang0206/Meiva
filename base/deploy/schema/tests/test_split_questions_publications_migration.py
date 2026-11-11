from __future__ import annotations

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import (
    SCHEMA_SQL,
    restore_pre_split_names,
    throwaway_database,
)
from base.paths.home import resolve_meiva_home

_MIGRATION = "20261111T193000_split-questions-and-publications"
_TABLES = (
    "agent_questions",
    "agent_publications",
    "agent_publication_seen",
    "keyed_operation_receipts",
)
_SHAPE = """
SELECT 'rel', c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND (c.relname LIKE 'agent_question%%'
    OR c.relname LIKE 'agent_publication%%' OR c.relname LIKE 'keyed_operation%%'
    OR c.relname LIKE 'agent_notice%%' OR c.relname LIKE 'agent_user_%%'
    OR c.relname LIKE 'notice_operation%%')
UNION ALL
SELECT 'con', conrelid::regclass::text || ':' || conname, pg_get_constraintdef(oid)
  FROM pg_constraint WHERE conrelid::regclass::text = ANY(%s)
UNION ALL
SELECT 'idx', indexname, replace(indexdef, tablename, 'T') FROM pg_indexes
 WHERE tablename = ANY(%s)
UNION ALL
SELECT 'col', table_name || '.' || column_name,
       data_type || ':' || is_nullable || ':' || coalesce(column_default, '')
  FROM information_schema.columns WHERE table_name = ANY(%s)
UNION ALL
SELECT 'cmt', c.relname, obj_description(c.oid) FROM pg_class c
 WHERE c.relname = 'agent_publication_seen'
ORDER BY 1, 2
"""


def _shape(url: str) -> list[tuple[str, str, str | None]]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            cast(LiteralString, _SHAPE), (list(_TABLES), list(_TABLES), list(_TABLES))
        ).fetchall()
    return [(str(a), str(b), None if c is None else str(c)) for a, b, c in rows]


def _previous_state(conn: psycopg.Connection) -> None:
    restore_pre_split_names(conn)
    conn.execute("DELETE FROM schema_migrations WHERE name=%s", (_MIGRATION,))
    conn.execute("INSERT INTO agents (id) VALUES (7)")
    conn.execute(
        "INSERT INTO agent_notices (agent_id, title, question, resolved_at, resolution, reply) VALUES "
        "(7, 'open', '{\"type\": \"text\"}', NULL, NULL, NULL), "
        "(7, 'answered', '{\"type\": \"text\"}', now(), 'answered', 'yes'), "
        "(7, 'expired', '{\"type\": \"text\"}', now(), 'expired', NULL), "
        "(7, 'superseded', '{\"type\": \"text\"}', now(), 'superseded', NULL), "
        "(7, 'withdrawn', '{\"type\": \"text\"}', now(), 'withdrawn', NULL), "
        "(7, 'dismissed', '{\"type\": \"text\"}', now(), 'dismissed', NULL), "
        "(7, 'legacy notice', NULL, now(), 'read', NULL)"
    )
    conn.execute(
        "INSERT INTO agent_user_messages (agent_id, source_key, mode, content, suggestions) "
        "VALUES (7, 'k', 'sdk', 'hello', '[]')"
    )
    conn.execute("INSERT INTO agent_user_seen (agent_id, seen_id) VALUES (7, 1)")
    conn.execute(
        "INSERT INTO notice_operation_receipts (path, operation_key, request, receipt) "
        "VALUES ('/p', 'k', '{}', '{}')"
    )
    conn.execute(
        "INSERT INTO inbound_messages (agent_id, kind, content, source) VALUES "
        "(7, 'chat', 'a', 'system:notice-reply'), (7, 'chat', 'b', 'system:notice-dismiss'), "
        "(7, 'chat', 'c', 'user')"
    )


# meiva:note
# The split migration renames the question, publication and receipt tables with their sequences,
# keys and constraints, deletes the historical rows that were not questions, and narrows what the
# question table accepts. An upgraded database ends with the same shape as a fresh one, ids and
# rows survive, and the inbound sources that named the old kind are rewritten.
def test_split_migration_keeps_rows_and_converges_with_a_fresh_baseline(
    db_url: str, machine_identity: MachineIdentityReader
) -> None:
    _ = db_url
    with (
        throwaway_database("split_old") as old,
        throwaway_database("split_fresh") as fresh,
    ):
        with psycopg.connect(old, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            _previous_state(conn)
        with psycopg.connect(fresh, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))

        with psycopg.connect(old) as conn:
            applied = apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
        assert _MIGRATION in applied
        assert _shape(old) == _shape(fresh)

        with psycopg.connect(old) as conn:
            assert conn.execute(
                "SELECT id, title, resolution FROM agent_questions ORDER BY id"
            ).fetchall() == [
                (1, "open", None),
                (2, "answered", "answered"),
                (3, "expired", "dismissed"),
                (4, "superseded", "dismissed"),
                (5, "withdrawn", "withdrawn"),
                (6, "dismissed", "dismissed"),
            ]
            assert conn.execute(
                "INSERT INTO agent_questions (agent_id, title, question) "
                "VALUES (7, 'next', '{\"type\": \"text\"}') RETURNING id"
            ).fetchone() == (8,)
            assert conn.execute(
                "SELECT content FROM agent_publications WHERE agent_id=7"
            ).fetchall() == [("hello",)]
            assert conn.execute("SELECT seen_id FROM agent_publication_seen").fetchall() == [(1,)]
            assert conn.execute("SELECT count(*) FROM keyed_operation_receipts").fetchone() == (1,)
            assert conn.execute(
                "SELECT source FROM inbound_messages WHERE agent_id=7 ORDER BY id"
            ).fetchall() == [("system:question-reply",), ("system:question-dismiss",), ("user",)]
            for statement in (
                "INSERT INTO agent_questions (agent_id, title, question, resolved_at, resolution) "
                "VALUES (7, 'x', '{\"type\": \"text\"}', now(), 'read')",
                "INSERT INTO agent_questions (agent_id, title, question) VALUES (7, 'x', NULL)",
            ):
                with pytest.raises(psycopg.errors.Error):
                    conn.execute(cast(LiteralString, statement))
                conn.rollback()
