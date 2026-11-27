# meiva:link cli/fleet_update/docs/structure.md

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from base.cluster import assert_checkpoint_dependency_pinned
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane.provision_plan import (
    CheckpointSchemaError,
    expected_checkpoint_schema_versions,
    verify_checkpoint_schema_versions,
)
from base.cluster.provision import CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS, checkpoint_schema_versions
from base.db import connect_url
from base.deploy.schema.migrations import (
    BASELINE_NAME,
    applied_migration_names,
    required_migration_set,
)


def schema_sets(url: str, migrations: Path) -> tuple[set[str], set[str]]:
    required = required_migration_set(migrations_dir=migrations)
    with connect_url(url, autocommit=True) as connection:
        applied = applied_migration_names(connection)
    return required, applied


def schema_version(names: set[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(names)).encode()).hexdigest()


def schema_status(required: set[str], applied: set[str], *, allow_pending: bool) -> str:
    if extra := applied - required:
        return f"schema ahead of target, unknown applied migrations {sorted(extra)}"
    if BASELINE_NAME not in applied:
        return "schema omits the target baseline"
    if missing := required - applied:
        if not allow_pending:
            return f"schema behind target, missing migrations {sorted(missing)}"
        return "migration-pending"
    return "ready"


def checkpoint_status(url: str, required: set[str], *, pending: set[str]) -> str:
    manifest = CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS
    upstream = PostgresCheckpointSchema().migrations
    try:
        assert_checkpoint_dependency_pinned(
            required, manifest=manifest, upstream_migrations=upstream
        )
        expected = expected_checkpoint_schema_versions(upstream, manifest)
        applied = checkpoint_schema_versions(url)
        if applied is not None and applied != expected:
            anticipated = {version for version, name in manifest.items() if name in pending}
            verify_checkpoint_schema_versions(applied | anticipated, expected)
            return "migration-pending"
        verify_checkpoint_schema_versions(applied, expected)
    except CheckpointSchemaError as exc:
        return str(exc)
    return "ready"
