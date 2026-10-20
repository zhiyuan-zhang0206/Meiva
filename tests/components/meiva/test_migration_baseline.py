"""Current baseline schema and durable trigger contracts."""

from __future__ import annotations

import os
import time
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.cluster.machine import MachineIdentity
from base.config import settings
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

_MIGRATIONS = SCHEMA_SQL.parents[1] / "migrations"


def test_fresh_schema_sql_bootstrap_is_baselined(machine_identity: MachineIdentity) -> None:
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
                apply_pending_migrations(conn, machine_name=machine_identity.name)
            ) == required_migration_set() - set(schema_sql_stamped_migration_names())
        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
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


def test_schema_sql_has_r1_deploy_state_tables() -> None:
    """Terminal-state pin (the pre-reset r1 deploy-state backfill contract):
    the baseline carries both deploy-state tables the rollout machinery reads."""
    schema = SCHEMA_SQL.read_text()
    for table in ("host_deploy_state",):
        assert f"CREATE TABLE {table}" in schema, f"{table} missing from baseline"


# ── Contraction of the retired deploy and watcher storage ──
#
# `20261001T055030_drop-retired-deploy-and-watcher-storage` drops the objects whose
# readers and writers are gone, and `20261001T055130_retire-restarting-and-publication-deferred`
# meiva:note
# tightens two `agents_meta` CHECKs (folded into the baseline).


def test_the_version_gate_and_posture_paths_survive_the_contraction(
    machine_identity: MachineIdentity,
) -> None:
    """Everything a live process still does against the two surviving tables."""
    with throwaway_database("retire_paths") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        conn.commit()
        apply_pending_migrations(conn, machine_name=machine_identity.name)
        conn.execute(
            "UPDATE deployment_state SET min_code_version = GREATEST(min_code_version, %s) "
            "WHERE id = 1",
            (2700,),
        )
        assert conn.execute(
            "SELECT COALESCE((SELECT min_code_version FROM public.deployment_state WHERE id = 1), 0)"
        ).fetchone() == (2700,)
        for posture in ("paused", "idle"):
            conn.execute(
                "INSERT INTO host_deploy_state (machine, posture, updated_at) "
                "VALUES ('m1', %s, now()) ON CONFLICT (machine) DO UPDATE "
                "SET posture = EXCLUDED.posture, updated_at = EXCLUDED.updated_at",
                (posture,),
            )
        assert conn.execute("SELECT posture FROM host_deploy_state").fetchone() == ("idle",)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE host_deploy_state SET posture = 'converging'")
        conn.rollback()
        conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine) VALUES (1, 'restarting', 'm')"
            )
        conn.rollback()


# ── Contraction of the delivery watchdog's durable retry state ──
#
# `20261011T191500_drop-dead-delivery-watchdog-state` drops the two watchdog
# tables and the three per-inbound dispatch columns nothing reads or writes any more.

_WATCHDOG_STATE = "20261011T191500_drop-dead-delivery-watchdog-state"
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


def test_a_fresh_database_has_none_of_the_dead_watchdog_state(
    machine_identity: MachineIdentity,
) -> None:
    with throwaway_database("watchdog_fresh") as url, psycopg.connect(url) as conn:
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        conn.commit()
        assert _watchdog_state_present(conn) == ([], [])
        apply_pending_migrations(conn, machine_name=machine_identity.name)
        assert _watchdog_state_present(conn) == ([], [])


def test_the_contraction_drops_the_state_of_an_upgraded_cluster_and_keeps_its_rows(
    machine_identity: MachineIdentity,
) -> None:
    """An older cluster carries the objects (and rows in them); the migration removes
    them, keeps every other inbound column, and replays as a no-op."""
    with throwaway_database("watchdog_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            conn.execute(
                "ALTER TABLE inbound_messages "
                "ADD COLUMN dispatch_count INT NOT NULL DEFAULT 0, "
                "ADD COLUMN last_dispatch_at TIMESTAMPTZ, "
                "ADD COLUMN poisoned_at TIMESTAMPTZ"
            )
            conn.execute(
                "CREATE TABLE delivery_watchdog_alerted ("
                "inbound_id BIGINT PRIMARY KEY REFERENCES inbound_messages(id) ON DELETE CASCADE, "
                "alerted_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            conn.execute(
                "CREATE TABLE delivery_watchdog_attempts ("
                "kind TEXT NOT NULL, agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE, "
                "last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(), "
                "consecutive_failures INT NOT NULL DEFAULT 0, suppress_count INT NOT NULL DEFAULT 0, "
                "PRIMARY KEY (kind, agent_id))"
            )
            conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
            conn.execute("INSERT INTO agents_meta (id, status, machine) VALUES (1, 'idling', 'm')")
            conn.execute(
                "INSERT INTO inbound_messages (id, agent_id, kind, content, dispatch_count) "
                "VALUES (10, 1, 'chat', 'hello', 3)"
            )
            conn.execute("INSERT INTO delivery_watchdog_alerted (inbound_id) VALUES (10)")
            conn.execute(
                "INSERT INTO delivery_watchdog_attempts (kind, agent_id) VALUES ('resurrect', 1)"
            )
            assert _watchdog_state_present(conn) == (
                list(_WATCHDOG_TABLES),
                sorted(_DISPATCH_COLUMNS),
            )
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_WATCHDOG_STATE,))

        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _WATCHDOG_STATE in applied
            assert _watchdog_state_present(conn) == ([], [])
            kept = conn.execute("SELECT content, status FROM inbound_messages WHERE id = 10")
            assert kept.fetchone() == ("hello", "pending")

        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
            migration = next(
                path for path in _MIGRATIONS.rglob(f"{_WATCHDOG_STATE}.sql")
            ).read_text()
            conn.execute(cast(LiteralString, migration))  # replay: every statement is IF EXISTS
            assert _watchdog_state_present(conn) == ([], [])


# meiva:note
# ── Retirement of the host fencing storage ──

_FENCING = "20261020T073712_retire-host-fencing-storage"
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


# meiva:note
# An older cluster carries the fencing columns, receipt table and done guard; the
# migration removes them, keeps `last_admission_at` and every row, and replays as a no-op.
def test_the_fencing_retirement_drops_an_upgraded_clusters_storage_and_keeps_its_rows(
    machine_identity: MachineIdentity,
) -> None:
    with throwaway_database("fencing_upgrade") as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
            conn.execute(
                "ALTER TABLE agents_meta "
                "ADD COLUMN incarnation_resources JSONB, "
                "ADD COLUMN last_admission_outcome TEXT, "
                "ADD COLUMN lease_expires_at TIMESTAMPTZ, "
                "ADD COLUMN lifecycle_command_id BIGINT, "
                "ADD COLUMN native_work_id BIGINT, "
                "ADD COLUMN pid INT, "
                "ADD COLUMN runtime_kind TEXT, "
                "ADD COLUMN started_at TIMESTAMPTZ"
            )
            conn.execute(
                "ALTER TABLE inbound_messages ADD COLUMN applied_at TIMESTAMPTZ, "
                "ADD COLUMN target_generation UUID, ADD COLUMN target_owner UUID"
            )
            conn.execute("CREATE TABLE native_restart_commands (id BIGINT PRIMARY KEY)")
            conn.execute(
                "CREATE FUNCTION reject_inbound_done_with_lifecycle_pointer() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
            )
            conn.execute(
                "CREATE TRIGGER inbound_messages_lifecycle_pointer_done_guard "
                "AFTER UPDATE ON inbound_messages FOR EACH ROW "
                "EXECUTE FUNCTION reject_inbound_done_with_lifecycle_pointer()"
            )
            conn.execute("INSERT INTO agents (id, label) VALUES (1, 'a')")
            conn.execute(
                "INSERT INTO agents_meta (id, status, machine, last_admission_at, "
                "runtime_kind, lease_expires_at) "
                "VALUES (1, 'idling', 'm', '2026-10-01T00:00:00Z', 'hosted', now())"
            )
            conn.execute(
                "INSERT INTO inbound_messages (id, agent_id, kind, content) "
                "VALUES (10, 1, 'chat', 'hello')"
            )
            assert _fencing_state_present(conn) == (
                sorted(_FENCING_AGENT_COLUMNS),
                sorted(_FENCING_INBOUND_COLUMNS),
                ["native_restart_commands"],
            )
            conn.execute("DELETE FROM schema_migrations WHERE name = %s", (_FENCING,))

        with psycopg.connect(url) as conn:
            applied = apply_pending_migrations(conn, machine_name=machine_identity.name)
            assert _FENCING in applied
            assert _fencing_state_present(conn) == ([], [], [])
            assert conn.execute(
                "SELECT count(*) FROM pg_trigger "
                "WHERE tgname = 'inbound_messages_lifecycle_pointer_done_guard'"
            ).fetchone() == (0,)
            assert conn.execute(
                "SELECT last_admission_at IS NOT NULL FROM agents_meta WHERE id = 1"
            ).fetchone() == (True,)
            assert conn.execute(
                "SELECT content FROM inbound_messages WHERE id = 10"
            ).fetchone() == ("hello",)

        with psycopg.connect(url) as conn:
            assert apply_pending_migrations(conn, machine_name=machine_identity.name) == []
            migration = next(path for path in _MIGRATIONS.rglob(f"{_FENCING}.sql")).read_text()
            conn.execute(cast(LiteralString, migration))
            assert _fencing_state_present(conn) == ([], [], [])
