"""Read-only diagnosis of the running image's required and applied migration sets."""

from __future__ import annotations

import logging
from collections.abc import Callable

import psycopg

from base.api_contracts.status import SchemaMismatchStatus
from base.db import Database
from base.deploy.schema.migration_errors import MigrationLayoutError
from base.deploy.schema.migration_layout import MIGRATIONS_DIR, required_migration_set
from base.deploy.schema.migrations import applied_migration_names
from ops.cluster_status.model import Mismatch as Mismatch
from ops.cluster_status.model import classify as classify

_log = logging.getLogger(__name__)


def detect(
    db: Database,
    *,
    conn: psycopg.Connection | None = None,
    applied_reader: Callable[[psycopg.Connection], set[str]] | None = None,
    required_reader: Callable[[], set[str]] | None = None,
) -> Mismatch | None:
    """Read applied names once; required names come from the running image's SQL."""
    read_applied = applied_reader or applied_migration_names
    try:
        if conn is None:
            with db.connect(autocommit=True) as borrowed:
                applied = read_applied(borrowed)
        else:
            applied = read_applied(conn)
    except MigrationLayoutError as exc:
        return Mismatch("invalid-migration-layout", f"DB migration catalog is invalid: {exc}")
    except psycopg.Error as exc:
        _log.warning("[schema-mismatch] DB facts unavailable: %r", exc)
        return Mismatch("unavailable", f"applied migration set unavailable ({type(exc).__name__})")
    try:
        required = (
            required_reader()
            if required_reader is not None
            else required_migration_set(migrations_dir=MIGRATIONS_DIR)
        )
    except MigrationLayoutError as exc:
        return Mismatch("invalid-migration-layout", f"image migration layout is invalid: {exc}")
    return classify(applied, required)


def status(
    db: Database,
    *,
    conn: psycopg.Connection | None = None,
    local_machine: str,
    applied_reader: Callable[[psycopg.Connection], set[str]] | None = None,
    required_reader: Callable[[], set[str]] | None = None,
) -> SchemaMismatchStatus | None:
    """Current diagnosis; return None only after both migration sets were read and match."""
    if applied_reader is None and required_reader is None:
        mismatch = detect(db) if conn is None else detect(db, conn=conn)
    elif conn is None:
        mismatch = detect(db, applied_reader=applied_reader, required_reader=required_reader)
    else:
        mismatch = detect(
            db, conn=conn, applied_reader=applied_reader, required_reader=required_reader
        )
    if mismatch is None:
        return None
    return SchemaMismatchStatus(kind=mismatch.kind, machine=local_machine, detail=mismatch.detail)
