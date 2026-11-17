# meiva:note
# Quota deadline expansion preserves existing card rows and fresh installs.

from typing import LiteralString, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database
from base.paths.home import resolve_meiva_home

_MIGRATION = "20261117T053248_plugin-stat-resets"


@pytest.mark.parametrize("upgrade", [False, True])
def test_plugin_stat_deadline_expansion(
    db_url: str, machine_identity: MachineIdentityReader, upgrade: bool
) -> None:
    _ = db_url
    with (
        throwaway_database("plugin_stat_resets") as url,
        psycopg.connect(url, autocommit=True) as conn,
    ):
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        if upgrade:
            conn.execute("ALTER TABLE plugin_stats DROP COLUMN resets")
        conn.execute("INSERT INTO plugin_stats (plugin, id, value) VALUES ('p', 'old', '40%')")
        apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )
        assert conn.execute(
            "SELECT value, resets FROM plugin_stats WHERE id = 'old'"
        ).fetchone() == ("40%", [])
        assert conn.execute(
            "SELECT name FROM schema_migrations WHERE name = %s", (_MIGRATION,)
        ).fetchone()
