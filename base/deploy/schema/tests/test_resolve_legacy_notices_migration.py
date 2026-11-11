from __future__ import annotations

from typing import LiteralString, cast

import psycopg

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_MIGRATION = "20261111T023500_resolve-legacy-notices"
_QUESTION = '{"type": "text"}'


def _notice(
    conn: psycopg.Connection,
    local_id: int,
    *,
    question: str | None,
    expire: bool = False,
    resolved: bool = False,
    require_response: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, "
        "question, expire_at, resolved_at, resolution) "
        "VALUES (1, %s, 'n', 'P2', %s, %s::jsonb, "
        "CASE WHEN %s THEN now() + interval '1 day' END, "
        "CASE WHEN %s THEN now() END, CASE WHEN %s THEN 'dismissed' END)",
        (local_id, require_response, question, expire, resolved, resolved),
    )


# meiva:note
# What is still open of the legacy notices closes as 'expired': non-question rows (of either
# require_response value) and rows that carried an expiry. A plain open question, and every row
# that was already resolved, stay as they were.
def test_open_legacy_notices_are_resolved_and_open_questions_are_kept(
    db_url: str, machine_identity: MachineIdentityReader
) -> None:
    _ = db_url
    with throwaway_database("resolve_legacy_notices") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            conn.execute("INSERT INTO agents (id) VALUES (1)")
            _notice(conn, 1, question=_QUESTION)
            _notice(conn, 2, question=None)
            _notice(conn, 3, question=None, require_response=False)
            _notice(conn, 4, question=_QUESTION, expire=True)
            _notice(conn, 5, question=_QUESTION, resolved=True)

        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(
                conn,
                machine_name=machine_identity.name,
                migrations_dir=MIGRATIONS_DIR,
                home=resolve_meiva_home(),
            )
            assert _MIGRATION in applied
            rows = dict(conn.execute("SELECT local_id, resolution FROM agent_notices").fetchall())
        assert rows == {1: None, 2: "expired", 3: "expired", 4: "expired", 5: "dismissed"}
