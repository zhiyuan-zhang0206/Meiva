from __future__ import annotations

from typing import LiteralString, cast

import psycopg

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_DROP_CAPABILITY_COLUMNS = "20261101T223334_drop-impersonation-capability-columns"
_RESTORE_COLUMNS = """
ALTER TABLE agent_impersonations
    ADD COLUMN capabilities JSONB,
    ADD COLUMN capability_declared_by TEXT
"""
_COLUMN_COUNT = """
SELECT count(*) FROM information_schema.columns
 WHERE table_schema = 'public'
   AND table_name = 'agent_impersonations'
   AND column_name = ANY(%s)
"""


def _apply(url: str, machine_identity: MachineIdentityReader) -> list[str]:
    with psycopg.connect(url) as conn:
        return apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )


def test_drop_capability_columns_removes_them_from_a_deployment_that_has_them(
    db_url: str,
    machine_identity: MachineIdentityReader,
) -> None:
    # meiva:note
    # The live deployments predate the 2026-11-01 baseline reset, so they still
    # carry both columns while `db/schema.sql` no longer creates them. Restoring
    # the columns after the baseline replays that shape, and the drop is what
    # makes the two agree. Remove the ALTER from the migration and this test
    # fails on the count, which is the point: a no-op run must not look like a
    # drop.
    _ = db_url
    with throwaway_database("drop_capability_columns") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            conn.execute(cast(LiteralString, _RESTORE_COLUMNS))
        applied = _apply(url, machine_identity)
        assert _DROP_CAPABILITY_COLUMNS in applied
        with psycopg.connect(url) as conn:
            assert conn.execute(
                cast(LiteralString, _COLUMN_COUNT),
                (["capabilities", "capability_declared_by"],),
            ).fetchone() == (0,)
        assert _apply(url, machine_identity) == []


def test_drop_capability_columns_is_a_no_op_on_a_fresh_database(
    db_url: str,
    machine_identity: MachineIdentityReader,
) -> None:
    # meiva:note
    # A fresh database builds from `db/schema.sql`, which already omits the
    # columns; the migration must still apply cleanly there, because that is the
    # shape every new database has when it reaches this file.
    _ = db_url
    with throwaway_database("drop_capability_columns_fresh") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        applied = _apply(url, machine_identity)
        assert _DROP_CAPABILITY_COLUMNS in applied
        with psycopg.connect(url) as conn:
            assert conn.execute(
                cast(LiteralString, _COLUMN_COUNT),
                (["capabilities", "capability_declared_by"],),
            ).fetchone() == (0,)
