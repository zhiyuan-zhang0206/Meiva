"""Current baseline schema and durable trigger contracts."""

from __future__ import annotations

import os
import time
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.cluster.machine import MachineIdentityReader
from base.config import settings
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import (
    apply_pending_migrations,
    required_migration_set,
)
from base.deploy.schema.tests.migration_support import (
    SCHEMA_SQL,
    schema_sql_stamped_migration_names,
    throwaway_database,
)
from base.deploy.schema.tests.migration_support import (
    reset_schema_migrations_state as reset_schema_migrations_state,
)
from base.paths.home import resolve_meiva_home


def test_fresh_schema_sql_bootstrap_is_baselined(machine_identity: MachineIdentityReader) -> None:
    """The real fresh-DB bootstrap: apply db/schema.sql to an empty database and
    assert it lands in the baselined applied-set state (new shape + baseline row,
    presets seeded), and apply_pending is then a no-op. Isolated throwaway DB, so
    this never touches the shared session DB."""
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    name = f"meiva_test_mig_{os.getpid()}_{int(time.time() * 1_000_000)}"
    url = f"{base_url}/{name}"
    schema = SCHEMA_SQL.read_text()

    with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(schema)  # type: ignore[arg-type]  # trusted multi-statement schema
            expected_stamped = {(name,) for name in schema_sql_stamped_migration_names()}
            row = conn.execute("SELECT name FROM schema_migrations").fetchall()
            assert set(row) == expected_stamped
            default_model = conn.execute(
                "SELECT llm_model FROM cluster_defaults WHERE id = 1"
            ).fetchone()
            assert default_model == ("deepseek-flash",)
        # Apply on the baselined DB: the folded migration marker makes the strict
        # ALTER skip a fresh schema that already carries the column; all other
        # post-baseline migrations replay cleanly, then a second apply is a no-op.
        with psycopg.connect(url) as conn:
            assert set(
                apply_pending_migrations(
                    conn,
                    machine_name=machine_identity.name,
                    migrations_dir=MIGRATIONS_DIR,
                    home=resolve_meiva_home(),
                )
            ) == required_migration_set(migrations_dir=MIGRATIONS_DIR) - set(
                schema_sql_stamped_migration_names()
            )
        with psycopg.connect(url) as conn:
            assert (
                apply_pending_migrations(
                    conn,
                    machine_name=machine_identity.name,
                    migrations_dir=MIGRATIONS_DIR,
                    home=resolve_meiva_home(),
                )
                == []
            )
            settled = conn.execute("SELECT llm_model FROM cluster_defaults WHERE id = 1").fetchone()
            assert settled == ("deepseek-flash",)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin, admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def test_schema_sql_has_birth_config_column() -> None:
    """Terminal-state pin (the pre-reset birth-config backfill contract, now
    folded into the baseline): agents_meta carries birth_config, and the
    CHECK-level comment in schema.sql documents the overlay-vs-default
    precedence the migration used to enforce by hand."""
    schema = SCHEMA_SQL.read_text()
    assert "birth_config               JSONB" in schema, (
        "birth_config column missing from agents_meta"
    )


# ── Contraction of the retired deploy and watcher storage ──
#
# `20261001T055030_drop-retired-deploy-and-watcher-storage` drops the objects whose
# readers and writers are gone, and `20261001T055130_retire-restarting-and-publication-deferred`
# meiva:note
# tightens two `agents_meta` CHECKs (folded into the baseline).


# meiva:note
# What a live process still does against the surviving `deployment_state` row and the
# tightened `agents_meta` status CHECK.
def test_the_version_gate_and_the_agent_status_check_survive_the_contraction(
    machine_identity: MachineIdentityReader,
) -> None:
    with throwaway_database("retire_paths") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        conn.commit()
        apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )
        conn.execute(
            "UPDATE deployment_state SET min_code_version = GREATEST(min_code_version, %s) "
            "WHERE id = 1",
            (2700,),
        )
        assert conn.execute(
            "SELECT COALESCE((SELECT min_code_version FROM public.deployment_state WHERE id = 1), 0)"
        ).fetchone() == (2700,)
        conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES (1, 'restarting', 'm')"
            )
        conn.rollback()


# ── Contraction of the delivery watchdog's durable retry state ──

_WATCHDOG_TABLES = ("delivery_watchdog_alerted", "delivery_watchdog_attempts")
_DISPATCH_COLUMNS = ("dispatch_count", "last_dispatch_at", "poisoned_at")


def _watchdog_state_present(conn: psycopg.Connection) -> tuple[list[str], list[str]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY 1",
            (list(_WATCHDOG_TABLES),),
        ).fetchall()
    ]
    columns = [
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'inbound_messages' "
            "AND column_name = ANY(%s) ORDER BY 1",
            (list(_DISPATCH_COLUMNS),),
        ).fetchall()
    ]
    return tables, columns


# meiva:note
# ── Retirement of the host fencing storage ──

_FENCING_AGENT_COLUMNS = (
    "incarnation_resources",
    "last_admission_outcome",
    "lease_expires_at",
    "lifecycle_command_id",
    "native_work_id",
    "pid",
    "runtime_kind",
    "started_at",
)
_FENCING_INBOUND_COLUMNS = ("applied_at", "target_generation", "target_owner")


def _fencing_state_present(conn: psycopg.Connection) -> tuple[list[str], list[str], list[str]]:
    def names(query: str, wanted: tuple[str, ...]) -> list[str]:
        return [
            row[0] for row in conn.execute(cast(LiteralString, query), (list(wanted),)).fetchall()
        ]

    columns = (
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = '{}' AND column_name = ANY(%s) ORDER BY 1"
    )
    return (
        names(columns.format("agents_meta"), _FENCING_AGENT_COLUMNS),
        names(columns.format("inbound_messages"), _FENCING_INBOUND_COLUMNS),
        names(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_name = ANY(%s) ORDER BY 1",
            ("native_restart_commands",),
        ),
    )


def test_a_fresh_database_has_none_of_the_dead_watchdog_state(
    machine_identity: MachineIdentityReader,
) -> None:
    with throwaway_database("watchdog_fresh") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        conn.commit()
        assert _watchdog_state_present(conn) == ([], [])
        assert _fencing_state_present(conn) == ([], [], [])
        apply_pending_migrations(
            conn,
            machine_name=machine_identity.name,
            migrations_dir=MIGRATIONS_DIR,
            home=resolve_meiva_home(),
        )
        assert _watchdog_state_present(conn) == ([], [])
        assert _fencing_state_present(conn) == ([], [], [])


# meiva:note
# Verify the folded baseline has dropped `host_deploy_state`.
def test_the_migration_drops_the_posture_table_of_an_upgraded_cluster(
    db_conn: psycopg.Connection,
) -> None:
    assert db_conn.execute("SELECT to_regclass('public.host_deploy_state')").fetchone() == (None,)


# meiva:note
# Verify the folded baseline has dropped the dead delivery-watchdog tables and dispatch columns.
def test_the_contraction_drops_the_state_of_an_upgraded_cluster_and_keeps_its_rows(
    db_conn: psycopg.Connection,
) -> None:
    assert _watchdog_state_present(db_conn) == ([], [])


# meiva:note
# Verify the folded baseline has dropped the retired host-fencing columns, table, and trigger.
def test_the_fencing_retirement_drops_an_upgraded_clusters_storage_and_keeps_its_rows(
    db_conn: psycopg.Connection,
) -> None:
    assert _fencing_state_present(db_conn) == ([], [], [])
    assert db_conn.execute(
        "SELECT count(*) FROM pg_trigger "
        "WHERE tgname = 'inbound_messages_lifecycle_pointer_done_guard'"
    ).fetchone() == (0,)
