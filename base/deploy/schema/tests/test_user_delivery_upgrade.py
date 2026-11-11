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
    restore_pre_split_names,
    throwaway_database,
)
from base.paths.home import resolve_meiva_home

_DELIVERY_MIGRATION = "20261106T032125_user-delivery-messages"
_NOTICE_COLUMN_MIGRATIONS = (
    "20261108T211435_add-notice-schedule-id",
    "20261111T144830_drop-agent-notices-legacy-columns",
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
    restore_pre_split_names(conn)
    conn.execute(
        "DROP TABLE reply_suggestion_feedback, reply_suggestion_offers, agent_user_messages"
    )
    restore_legacy_notice_columns(conn)
    conn.execute("ALTER TABLE agent_notices ALTER COLUMN expire_at SET NOT NULL")
    conn.execute(
        "DELETE FROM schema_migrations WHERE name = ANY(%s)",
        ([_DELIVERY_MIGRATION, *_NOTICE_COLUMN_MIGRATIONS],),
    )
    conn.execute("INSERT INTO agents (id) VALUES (42)")
    conn.execute(
        "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, "
        "expire_at, resolved_at, resolution, reply) VALUES "
        "(42, 1, 'Pending question', 'P2', true, now()+interval '1 day', NULL, NULL, NULL), "
        "(42, 2, 'Answered question', 'P2', true, now()+interval '1 day', now(), 'answered', 'Keep it')"
    )


# meiva:note
# Fresh databases already have the new tables. An upgrade must discover the shipped
# migration through the runtime's canonical directory and preserve existing questions.
@pytest.mark.parametrize("upgrade", [False, True])
def test_user_delivery_migrates_existing_databases_and_skips_fresh_baselines(
    db_url: str,
    machine_identity: MachineIdentityReader,
    upgrade: bool,
) -> None:
    _ = db_url
    with throwaway_database("user_delivery_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            if upgrade:
                _restore_previous_schema(conn)

        applied = _apply(url, machine_identity)
        assert (_DELIVERY_MIGRATION in applied) is upgrade
        with psycopg.connect(url) as conn:
            assert conn.execute(
                "SELECT to_regclass(%s)::text, "
                "to_regclass('reply_suggestion_offers')::text, "
                "to_regclass('reply_suggestion_feedback')::text",
                ("agent_user_messages" if upgrade else "agent_publications",),
            ).fetchone() == (
                "agent_user_messages" if upgrade else "agent_publications",
                "reply_suggestion_offers",
                "reply_suggestion_feedback",
            )
            assert (
                conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name=%s AND column_name = ANY(%s)",
                    (
                        "agent_notices" if upgrade else "agent_questions",
                        list(LEGACY_NOTICE_COLUMNS),
                    ),
                ).fetchall()
                == []
            )
            if upgrade:
                assert conn.execute(
                    "SELECT title, question, reply FROM agent_notices "
                    "WHERE agent_id=42 ORDER BY title"
                ).fetchall() == [
                    ("Answered question", None, "Keep it"),
                    ("Pending question", {"type": "text"}, None),
                ]
        assert _apply(url, machine_identity) == []
