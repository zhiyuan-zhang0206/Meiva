from __future__ import annotations

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_DELIVERY_MIGRATION = "20261106T032125_user-delivery-messages"


def _apply(url: str, machine_identity: MachineIdentityReader) -> list[str]:
    with psycopg.connect(url) as conn:
        return apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )


def _restore_previous_schema(conn: psycopg.Connection) -> None:
    conn.execute(
        "DROP TABLE reply_suggestion_feedback, reply_suggestion_offers, agent_user_messages"
    )
    conn.execute("ALTER TABLE agent_notices ALTER COLUMN expire_at SET NOT NULL")
    conn.execute("DELETE FROM schema_migrations WHERE name=%s", (_DELIVERY_MIGRATION,))
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
                "SELECT to_regclass('agent_user_messages')::text, "
                "to_regclass('reply_suggestion_offers')::text, "
                "to_regclass('reply_suggestion_feedback')::text"
            ).fetchone() == (
                "agent_user_messages",
                "reply_suggestion_offers",
                "reply_suggestion_feedback",
            )
            assert conn.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='agent_notices' AND column_name='expire_at'"
            ).fetchone() == ("YES",)
            if upgrade:
                assert conn.execute(
                    "SELECT local_id, expire_at IS NULL, question, reply FROM agent_notices "
                    "WHERE agent_id=42 ORDER BY local_id"
                ).fetchall() == [(1, True, {"type": "text"}, None), (2, False, None, "Keep it")]
        assert _apply(url, machine_identity) == []
