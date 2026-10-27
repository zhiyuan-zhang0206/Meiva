# meiva:note
# Pending-migration applier — a step of `meiva start`, not a standalone command.
#
# There is no `meiva migrations` CLI surface. `cmd_migrations_apply` runs from `start/__init__.py`
# early in boot (after pg is up, before the schema-current assertion); long-running daemons
# additionally call `assert_schema_current` on their own entry points.

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg

from base.cluster.checkpoint_schema import CheckpointSchema, PostgresCheckpointSchema
from base.cluster.machine import MachineIdentityReader
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.lifecycle.start.cold_start_plan import (
    MigrationAuthority,
    migration_authority,
)
from cli.database import OperatorDatabaseFactory

_DEFAULT_CHECKPOINT_SCHEMA: CheckpointSchema = PostgresCheckpointSchema()


# meiva:note
# Apply Meiva migrations and verify checkpoint schema; `meiva start` step 2.5.
#
# Meiva SQL files run on every host (a runner normally has nothing pending and
# the authority guard refuses it if it does). Every host then verifies the
# complete LangGraph checkpoint migration set read-only. Runtime checkpoint
# readers and agent boot may dial as ``meiva_runner`` and never perform DDL.
#
# Each Meiva migration runs in a single transaction. `meiva start` invokes this
# after pg is ready; on the gateway the fleet update reaches it through
# the trailing `meiva start`.
#
# Returns the names applied, NOT an exit code. Its caller needs the set,
# because a migration that created a table is the moment `meiva_runner`'s
# point-in-time read grant went stale; failure is raised, not returned.
def cmd_migrations_apply(
    *,
    database_factory: OperatorDatabaseFactory,
    machine: MachineIdentityReader,
    config: DataPlaneConfig,
    home: Path,
    checkpoint_schema: CheckpointSchema = _DEFAULT_CHECKPOINT_SCHEMA,
) -> list[str]:
    from base import cluster
    from base.cluster.dataplane import pg_admin
    from base.cluster.provision import CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS
    from base.deploy.schema.migration_layout import required_migration_set

    # meiva:note
    # Dependency drift is a pre-DB gate: a new upstream checkpoint migration
    # must first be mirrored in an Meiva migration. Failing before
    # Meiva SQL runs keeps update recovery on the old code + old schema.
    cluster.assert_checkpoint_dependency_pinned(
        required_migration_set(migrations_dir=MIGRATIONS_DIR),
        manifest=CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS,
        upstream_migrations=checkpoint_schema.migrations,
    )

    if migration_authority(remote_plane=config.is_remote) is MigrationAuthority.ACCESS_URL:
        with database_factory().connect(unbounded=True) as conn:
            done = _apply(conn, machine=machine, home=home)
        cluster.assert_checkpoint_schema_current(database_factory.db_url())
    else:
        authority = pg_admin.local_owner_authority(config.db_url, remote_plane=False, home=home)
        with authority.session() as conn:
            done = _apply(conn, machine=machine, home=home)
        cluster.assert_checkpoint_schema_current(
            authority.conninfo, expected_data_dir=authority.data_dir
        )
    print(f"applied {len(done)} migration(s): {done}")
    return done


def _apply(
    conn: psycopg.Connection[Any], *, machine: MachineIdentityReader, home: Path
) -> list[str]:
    from base.deploy.schema.migrations import apply_pending_migrations

    return apply_pending_migrations(
        conn, machine_name=machine.name, migrations_dir=MIGRATIONS_DIR, home=home
    )
