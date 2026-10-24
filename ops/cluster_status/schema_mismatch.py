# meiva:note
# Read-only diagnosis of the running image's required and applied migration sets.
#
# The applied names come from the database (one query on the connection it is given, or on one
# it borrows), the required names from the running image's migration directory. Reading is this
# module's job; comparing the two sets and wording a failure to read them are decided in
# `ops.cluster_status.model`.

from __future__ import annotations

import logging
from pathlib import Path

import psycopg

from base.api_contracts.status import SchemaMismatchStatus
from base.db import Database
from base.deploy.schema.migration_errors import MigrationLayoutError
from base.deploy.schema.migration_layout import MIGRATIONS_DIR, required_migration_set
from base.deploy.schema.migrations import applied_migration_names
from ops.cluster_status.model import Mismatch as Mismatch
from ops.cluster_status.model import SchemaReadFailure, read_failure_mismatch
from ops.cluster_status.model import classify as classify

_log = logging.getLogger(__name__)


def _diagnose(
    db: Database, conn: psycopg.Connection | None, migrations_dir: Path
) -> Mismatch | None:
    try:
        if conn is None:
            with db.connect(autocommit=True) as borrowed:
                applied = applied_migration_names(borrowed)
        else:
            applied = applied_migration_names(conn)
    except MigrationLayoutError as exc:
        return read_failure_mismatch(SchemaReadFailure.DB_CATALOG_INVALID, str(exc))
    except psycopg.Error as exc:
        _log.warning("[schema-mismatch] DB facts unavailable: %r", exc)
        return read_failure_mismatch(SchemaReadFailure.DB_UNAVAILABLE, type(exc).__name__)
    try:
        required = required_migration_set(migrations_dir=migrations_dir)
    except MigrationLayoutError as exc:
        return read_failure_mismatch(SchemaReadFailure.IMAGE_LAYOUT_INVALID, str(exc))
    return classify(applied, required)


# meiva:note
# The current diagnosis, or None only after both migration sets were read and match.
#
# `migrations_dir` is the running image's migration directory; every production caller reads
# the checkout it runs from, so the default is that directory and only a test of a broken image
# layout names another. Anything that is neither a malformed layout nor a database error is a
# defect in the reader and propagates.
def status(
    db: Database,
    *,
    conn: psycopg.Connection | None = None,
    local_machine: str,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> SchemaMismatchStatus | None:
    mismatch = _diagnose(db, conn, migrations_dir)
    if mismatch is None:
        return None
    return SchemaMismatchStatus(kind=mismatch.kind, machine=local_machine, detail=mismatch.detail)
