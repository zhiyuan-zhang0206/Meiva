from __future__ import annotations

from typing import LiteralString, cast

import psycopg

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import (
    SCHEMA_SQL,
    restore_pre_split_names,
    throwaway_database,
)
from base.paths.home import resolve_meiva_home

_SEEN_MIGRATION = "20261110T153500_agent-user-seen"


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
    conn.execute("ALTER TABLE agent_user_messages ADD COLUMN read_at TIMESTAMPTZ")
    conn.execute("DROP TABLE agent_user_seen")
    conn.execute("DROP INDEX agent_user_messages_by_agent")
    conn.execute("DELETE FROM schema_migrations WHERE name=%s", (_SEEN_MIGRATION,))


def _record(
    conn: psycopg.Connection, agent: int, key: str, *, read: bool, content: bool = True
) -> None:
    conn.execute(
        "INSERT INTO agent_user_messages (agent_id, source_key, mode, content, suggestions, read_at) "
        "VALUES (%s, %s, 'prefix', %s, '[]', CASE WHEN %s THEN now() END)",
        (agent, key, "Done" if content else None, read),
    )


# meiva:note
# An upgrade keeps each agent's unread state: the cursor stops just before its oldest unread
# delivery, sits at its newest delivery when everything was read, and is absent when it has none.
# The baseline no longer has `read_at` (it stamps this migration as folded), so the test rebuilds
# the schema this migration upgrades. The drop migration is stamped as folded for the same reason,
# so the column the test put back stays.
def test_seen_cursor_is_backfilled_from_per_record_read_state(
    db_url: str, machine_identity: MachineIdentityReader
) -> None:
    _ = db_url
    with throwaway_database("agent_user_seen_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            _restore_previous_schema(conn)
            for agent in (1, 2, 3, 4):
                conn.execute("INSERT INTO agents (id) VALUES (%s)", (agent,))
            _record(conn, 1, "a", read=True)
            _record(conn, 1, "b", read=True)
            _record(conn, 1, "c", read=False)
            _record(conn, 1, "d", read=False)
            _record(conn, 2, "a", read=True)
            _record(conn, 3, "a", read=False)
            _record(conn, 3, "silent", read=False, content=False)

        applied = _apply(url, machine_identity)
        assert _SEEN_MIGRATION in applied
        with psycopg.connect(url) as conn:
            ids = {
                (agent, key): row_id
                for row_id, agent, key in conn.execute(
                    "SELECT id, agent_id, source_key FROM agent_user_messages"
                ).fetchall()
            }
            seen = dict(conn.execute("SELECT agent_id, seen_id FROM agent_user_seen").fetchall())
            index = conn.execute(
                "SELECT to_regclass('agent_user_messages_by_agent')::text"
            ).fetchone()
        assert index == ("agent_user_messages_by_agent",)
        assert seen == {
            1: ids[(1, "c")] - 1,
            2: ids[(2, "a")],
            3: ids[(3, "a")] - 1,
        }
        assert _apply(url, machine_identity) == []
