# meiva:note
# Current schema status must work from an installed image without Git or watchdog history.
#
# The two migration sets are the real ones: the throwaway database's applied names, and the
# running image's migration directory. A mismatch is made by changing the applied set inside a
# rolled-back transaction, an unreadable database by a closed connection or a held table lock,
# and a broken image by naming a directory that is not there.

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest

from base.api_contracts.status import MachineStatus, SchemaMismatchKind, SchemaMismatchStatus
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.db import Database
from base.deploy.schema.migration_errors import MigrationLayoutError
from base.deploy.schema.migrations import applied_migration_names
from cli.commands.cluster.plans.roster_table import schema_mismatch_banner
from ops.cluster_status import ClusterStatus, schema_mismatch


@pytest.mark.parametrize(
    ("applied", "required", "kind"),
    [
        ({"base"}, {"base"}, None),
        ({"old"}, {"new"}, "divergent"),
        ({"base", "new"}, {"base"}, "schema-ahead-of-code"),
        ({"base"}, {"base", "new"}, "schema-behind-code"),
    ],
)
def test_current_sets_decide_schema_status(
    applied: set[str], required: set[str], kind: str | None
) -> None:
    mismatch = schema_mismatch.classify(applied, required)
    assert (mismatch.kind if mismatch else None) == kind


def _assert_status_is_visible(status: SchemaMismatchStatus) -> None:
    local = ClusterStatus(
        machine_name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        paused=False,
        schema_mismatch=status,
    )
    assert local.model_dump(mode="json")["schema_mismatch"] == status.model_dump()
    machine = MachineStatus(
        name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        gateway_url="http://gateway",
        up_since_at=datetime(2026, 9, 26, tzinfo=UTC),
        online=True,
        paused=False,
        schema_mismatch=status,
    )
    banner = schema_mismatch_banner([machine])
    assert len(banner) == 1
    assert status.kind in banner[0]
    assert status.detail in banner[0]
    assert "blocked round" not in banner[0] and "held back" not in banner[0]


_EXTRA_MIGRATION = "99999999T999999_extra"


def _newest_applied(conn: psycopg.Connection) -> str:
    return max(applied_migration_names(conn))


@pytest.mark.parametrize(
    ("kind", "detail"),
    [
        ("schema-ahead-of-code", "DB has 1 migration(s) this image lacks"),
        ("schema-behind-code", "DB lacks 1 migration(s) required by this image"),
        (
            "divergent",
            "DB has 1 migration(s) this image lacks and lacks 1 required by this image",
        ),
    ],
)
def test_status_and_cli_expose_current_diagnosis_only(
    db_conn: psycopg.Connection,
    database: Database,
    kind: SchemaMismatchKind,
    detail: str,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="gateway")
    with db_conn.transaction(force_rollback=True):
        if kind != "schema-ahead-of-code":
            db_conn.execute(
                "DELETE FROM schema_migrations WHERE name = %s", (_newest_applied(db_conn),)
            )
        if kind != "schema-behind-code":
            db_conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (_EXTRA_MIGRATION,))
        status = schema_mismatch.status(
            database, conn=db_conn, local_machine=machine_identity.name()
        )

    assert status is not None
    assert status.model_dump() == {"kind": kind, "machine": "gateway", "detail": detail}
    _assert_status_is_visible(status)


def test_malformed_real_catalog_is_invalid_in_status_and_cli(
    db_conn: psycopg.Connection,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    original = applied_migration_names(db_conn)
    assert (
        schema_mismatch.status(database, conn=db_conn, local_machine=machine_identity.name())
        is None
    )

    with db_conn.transaction(force_rollback=True):
        db_conn.execute("ALTER TABLE schema_migrations RENAME COLUMN name TO unexpected_name")
        with pytest.raises(MigrationLayoutError, match="unrecognized shape"):
            applied_migration_names(db_conn)
        status = schema_mismatch.status(
            database, conn=db_conn, local_machine=machine_identity.name()
        )
        assert status is not None
        assert status.kind == "invalid-migration-layout"
        assert "DB migration catalog is invalid" in status.detail
        assert "unrecognized shape" in status.detail
        _assert_status_is_visible(status)

    assert applied_migration_names(db_conn) == original
    assert (
        schema_mismatch.status(database, conn=db_conn, local_machine=machine_identity.name())
        is None
    )


def test_a_closed_connection_is_explicitly_unavailable(
    db_url: str, database: Database, *, machine_identity: MachineIdentityReader
) -> None:
    conn = psycopg.connect(db_url)
    conn.close()

    status = schema_mismatch.status(database, conn=conn, local_machine=machine_identity.name())

    assert status is not None
    assert status.kind == "unavailable"
    assert "OperationalError" in status.detail
    _assert_status_is_visible(status)


# meiva:note
# A query the server cancels (here: the registry read waits on a table lock past the statement
# timeout) is an unavailable diagnosis, not a mismatch and not a crash.
def test_a_query_the_server_cancels_is_explicitly_unavailable(
    db_url: str,
    db_conn: psycopg.Connection,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with psycopg.connect(db_url) as holder:
        holder.execute("LOCK TABLE schema_migrations IN ACCESS EXCLUSIVE MODE")
        db_conn.execute("SET statement_timeout = '200ms'")

        status = schema_mismatch.status(
            database, conn=db_conn, local_machine=machine_identity.name()
        )

    assert status is not None
    assert status.kind == "unavailable"
    assert "QueryCanceled" in status.detail
    _assert_status_is_visible(status)


def test_invalid_image_layout_is_not_a_set_divergence(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    status = schema_mismatch.status(
        database,
        conn=db_conn,
        local_machine=machine_identity.name(),
        migrations_dir=tmp_path / "absent",
    )
    assert status is not None
    assert status.kind == "invalid-migration-layout"
    assert "image migration layout is invalid" in status.detail
    assert "migrations dir does not exist" in status.detail
    _assert_status_is_visible(status)


def test_unexpected_programming_failure_is_not_silently_graded(database: Database) -> None:
    with pytest.raises(AttributeError):
        schema_mismatch.status(
            database, conn=cast(psycopg.Connection, object()), local_machine="gateway"
        )


@pytest.mark.parametrize("kind", ["invalid-migration-layout", "unavailable"])
def test_gateway_roster_row_preserves_degraded_schema_status(kind: SchemaMismatchKind) -> None:
    from gateway.cluster.roster.roster_probe import probed_facts
    from gateway.cluster.roster.roster_rows import RosterRowSpec, reported_status

    diagnosis = SchemaMismatchStatus(kind=kind, machine="gateway", detail="schema evidence failed")
    response = ClusterStatus(
        machine_name="gateway",
        serve_gateway=True,
        serve_agent_runner=False,
        paused=False,
        schema_mismatch=diagnosis,
    )

    row = reported_status(
        RosterRowSpec(
            name="gateway",
            gateway_url="http://inert.invalid",
            role=["gateway"],
            up_since_at=datetime.now(UTC),
            description=None,
            stopped_at=None,
        ),
        probed_facts(response),
        observed_at=None,
    )
    assert row.online is True
    assert row.schema_mismatch is not None
    assert row.schema_mismatch == diagnosis
    assert row.model_dump(mode="json")["schema_mismatch"] == diagnosis.model_dump()
    _assert_status_is_visible(row.schema_mismatch)
