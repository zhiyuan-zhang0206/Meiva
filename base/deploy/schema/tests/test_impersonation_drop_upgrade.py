from __future__ import annotations

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_CLEAN_MIGRATION = "20261107T103459_delete-inbound-reminders"
_DROP_MIGRATION = "20261107T103500_drop-agent-impersonations"


def _apply(url: str, machine_identity: MachineIdentityReader) -> list[str]:
    with psycopg.connect(url) as conn:
        return apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )


def _restore_previous_schema(conn: psycopg.Connection) -> None:
    _ = conn.execute(
        "ALTER TABLE public.inbound_messages DROP CONSTRAINT IF EXISTS inbound_messages_kind_check"
    )
    _ = conn.execute(
        "ALTER TABLE public.inbound_messages ADD CONSTRAINT inbound_messages_kind_check "
        "CHECK (kind IN ('chat', 'system_note', 'compact_summary', 'compact_request', "
        "'cancel', 'terminate', 'restart', 'resurrect', 'fork', 'heartbeat', 'reminder'))"
    )
    _ = conn.execute("INSERT INTO agents (id) VALUES (42) ON CONFLICT DO NOTHING")
    _ = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, status) VALUES "
        "(42, 'Lease expiration reminder', 'reminder', 'pending'), "
        "(42, 'Normal chat message', 'chat', 'pending')"
    )
    _ = conn.execute(
        "DELETE FROM schema_migrations WHERE name IN (%s, %s)", (_CLEAN_MIGRATION, _DROP_MIGRATION)
    )


# meiva:note
# Fresh databases skip both migrations because they are stamped in schema.sql.
# An upgrade against a database carrying legacy 'reminder' inbound rows must clean
# them via 20261107T103459 so 20261107T103500 constraint tightening succeeds.
@pytest.mark.parametrize("upgrade", [False, True])
def test_impersonation_drop_migrates_existing_databases_and_skips_fresh_baselines(
    db_url: str,
    machine_identity: MachineIdentityReader,
    upgrade: bool,
) -> None:
    _ = db_url
    with throwaway_database("impersonation_drop_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            _ = conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            if upgrade:
                _restore_previous_schema(conn)

        applied = _apply(url, machine_identity)
        assert (_CLEAN_MIGRATION in applied) is upgrade
        assert (_DROP_MIGRATION in applied) is upgrade

        with psycopg.connect(url) as conn:
            assert conn.execute("SELECT to_regclass('agent_impersonations')").fetchone() == (None,)
            kinds = [
                r[0]
                for r in conn.execute(
                    "SELECT kind FROM inbound_messages WHERE agent_id=42"
                ).fetchall()
            ]
            if upgrade:
                assert "reminder" not in kinds
                assert "chat" in kinds
            with pytest.raises(psycopg.errors.CheckViolation):
                _ = conn.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (42, 'x', 'reminder')"
                )

        assert _apply(url, machine_identity) == []
