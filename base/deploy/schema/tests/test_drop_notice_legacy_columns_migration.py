from __future__ import annotations

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import (
    LEGACY_NOTICE_COLUMNS,
    SCHEMA_SQL,
    restore_legacy_notice_columns,
    throwaway_database,
)
from base.paths.home import resolve_meiva_home

_MIGRATION = "20261111T144830_drop-agent-notices-legacy-columns"
_LEGACY_CONSTRAINTS = (
    "agent_notices_question_requires_response",
    "agent_notices_agent_local_id_unique",
    "agent_notices_blocking_requires_response",
    "agent_notices_resolution_legal",
    "agent_notices_task_id_fkey",
)


def _apply(url: str, machine_identity: MachineIdentityReader) -> list[str]:
    with psycopg.connect(url) as conn:
        return apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )


def _restore_previous_schema(conn: psycopg.Connection) -> None:
    restore_legacy_notice_columns(conn)
    conn.execute("DELETE FROM schema_migrations WHERE name=%s", (_MIGRATION,))
    conn.execute(
        "ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_agent_local_id_unique UNIQUE (agent_id, local_id)"
    )
    conn.execute(
        "ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_resolution_legal CHECK ("
        "resolution IS NULL OR (require_response AND resolution <> 'read') OR NOT require_response)"
    )
    conn.execute("ALTER TABLE agent_notices DROP CONSTRAINT agent_notices_question_is_object")
    conn.execute(
        "ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_question_requires_response "
        "CHECK (question IS NULL OR (jsonb_typeof(question) = 'object' AND require_response))"
    )
    conn.execute(
        "ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_blocking_requires_response "
        "CHECK (NOT blocking OR require_response)"
    )
    conn.execute(
        "ALTER TABLE agent_notices ADD CONSTRAINT agent_notices_task_id_fkey "
        "FOREIGN KEY (task_id) REFERENCES agent_tasks(id)"
    )
    conn.execute("INSERT INTO agents (id) VALUES (1)")
    conn.execute(
        "INSERT INTO agent_notices "
        "(agent_id, local_id, title, question, require_response, resolved_at, resolution) "
        "VALUES (1, 1, 'open', '{\"type\": \"text\"}', true, NULL, NULL), "
        "(1, 2, 'read', NULL, false, now(), 'read'), "
        "(1, 3, 'expired', NULL, true, now(), 'expired')"
    )


# meiva:note
# The contract step removes the seven columns the expand step stopped using, with the constraints
# that existed for them, and keeps every row. The resolution values old rows carry stay legal, and
# the question payload shape check that lived in a require_response constraint survives on its own.
# The baseline no longer has the columns, so an upgrade is a baseline with them put back; on a
# fresh baseline the migration replays as a no-op. Every migration replayed over the baseline lands
# on the baseline's column set; schedule_id, which 20261108T211435 re-adds on a fresh database, is
# part of what this migration removes.
@pytest.mark.parametrize("upgrade", [False, True])
def test_legacy_notice_columns_are_dropped_and_rows_survive(
    db_url: str, machine_identity: MachineIdentityReader, upgrade: bool
) -> None:
    _ = db_url
    with throwaway_database("drop_notice_legacy_columns") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            if upgrade:
                _restore_previous_schema(conn)

        applied = _apply(url, machine_identity)
        assert _MIGRATION in applied
        with psycopg.connect(url) as conn:
            assert (
                conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='agent_notices' AND column_name = ANY(%s)",
                    (list(LEGACY_NOTICE_COLUMNS),),
                ).fetchall()
                == []
            )
            assert {
                name
                for (name,) in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='agent_notices'"
                ).fetchall()
            } == {
                "id",
                "agent_id",
                "title",
                "content",
                "created_at",
                "resolved_at",
                "resolution",
                "reply",
                "question",
                "answer",
                "source_verified_by",
            }
            assert (
                conn.execute(
                    "SELECT conname FROM pg_constraint WHERE conrelid='agent_notices'::regclass "
                    "AND conname = ANY(%s)",
                    (list(_LEGACY_CONSTRAINTS),),
                ).fetchall()
                == []
            )
            assert conn.execute(
                "SELECT count(*) FROM pg_constraint WHERE conrelid='agent_notices'::regclass "
                "AND conname='agent_notices_question_is_object'"
            ).fetchone() == (1,)
            if upgrade:
                assert conn.execute(
                    "SELECT title, resolution FROM agent_notices ORDER BY id"
                ).fetchall() == [("open", None), ("read", "read"), ("expired", "expired")]
            conn.execute("INSERT INTO agents (id) VALUES (2)")
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO agent_notices (agent_id, title, question) VALUES (2, 'bad', '[]')"
                )
        assert _apply(url, machine_identity) == []
