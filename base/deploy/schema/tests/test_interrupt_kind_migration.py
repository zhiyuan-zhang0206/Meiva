from __future__ import annotations

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_MIGRATION = "20261111T183534_rename-inbound-kind-cancel-to-interrupt"


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
        "'cancel', 'terminate', 'restart', 'resurrect', 'fork', 'heartbeat'))"
    )
    _ = conn.execute("INSERT INTO agents (id) VALUES (42) ON CONFLICT DO NOTHING")
    _ = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status) VALUES "
        "(42, '', 'cancel', 'user', 'done'), (42, '', 'cancel', 'user', 'pending'), "
        "(42, 'hello', 'chat', 'user', 'pending')"
    )
    _ = conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_MIGRATION,))


# meiva:note
# An upgrade turns every stored `cancel` row into `interrupt` (pending ones included, so the
# interrupt a runner had not yet consumed is still one) and the constraint no longer admits the old
# name. A fresh database already carries the new constraint and replays the idempotent migration harmlessly.
@pytest.mark.parametrize("upgrade", [False, True])
def test_the_cancel_inbound_kind_becomes_interrupt(
    db_url: str, machine_identity: MachineIdentityReader, upgrade: bool
) -> None:
    _ = db_url
    with throwaway_database("interrupt_kind_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            _ = conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            if upgrade:
                _restore_previous_schema(conn)

        applied = _apply(url, machine_identity)
        assert _MIGRATION in applied

        with psycopg.connect(url) as conn:
            if upgrade:
                rows = conn.execute(
                    "SELECT kind, status FROM inbound_messages WHERE agent_id = 42 ORDER BY id"
                ).fetchall()
                assert rows == [
                    ("interrupt", "done"),
                    ("interrupt", "pending"),
                    ("chat", "pending"),
                ]
            conn.execute("INSERT INTO agents (id) VALUES (43) ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (43, '', 'interrupt', 'user')"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                    "VALUES (43, '', 'cancel', 'user')"
                )
