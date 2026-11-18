from __future__ import annotations

from typing import Any, LiteralString, cast

import psycopg
import pytest

from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.tests.migration_support import SCHEMA_SQL, throwaway_database

_MIGRATION = next(MIGRATIONS_DIR.rglob("20261117T100900_drop-schedule-daemon-trigger.sql"))


def _schedule_contract(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='schedules'::regclass AND conname LIKE 'schedules_trigger_%' "
        "ORDER BY conname"
    ).fetchall()


def _restore_daemon_schema(conn: psycopg.Connection) -> None:
    conn.execute(
        "ALTER TABLE schedules DROP CONSTRAINT schedules_trigger_kind_check, "
        "DROP CONSTRAINT schedules_trigger_payload_check, "
        "ALTER COLUMN trigger_kind SET DEFAULT 'daemon', "
        "ADD CONSTRAINT schedules_trigger_kind_check "
        "CHECK (trigger_kind IN ('at', 'cron', 'every', 'daemon'))"
    )


def test_declarative_schema_matches_upgrade_and_rejects_daemon(db_url: str) -> None:
    _ = db_url
    with (
        throwaway_database("schedule_daemon_retirement") as url,
        psycopg.connect(url, autocommit=True) as conn,
    ):
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        baseline = _schedule_contract(conn)
        _restore_daemon_schema(conn)
        conn.execute(cast(LiteralString, _MIGRATION.read_text()))
        assert _schedule_contract(conn) == baseline
        assert conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='schedules' AND column_name='trigger_kind'"
        ).fetchone() == (None,)
        conn.execute(
            "INSERT INTO schedules (name, script, command, machine, trigger_kind, run_at) "
            "VALUES ('once', 'pass', 'python run.py', 'test', 'at', now())"
        )
        with pytest.raises(psycopg.errors.CheckViolation) as rejected:
            conn.execute(
                "INSERT INTO schedules (name, script, command, machine, trigger_kind) "
                "VALUES ('resident', 'pass', 'python run.py', 'test', 'daemon')"
            )
        assert rejected.value.diag.constraint_name == "schedules_trigger_kind_check"


def test_upgrade_preserves_unconverted_rows_and_refuses_retirement(db_url: str) -> None:
    _ = db_url
    with (
        throwaway_database("schedule_daemon_refusal") as url,
        psycopg.connect(url, autocommit=True) as conn,
    ):
        conn.execute(cast(LiteralString, SCHEMA_SQL.read_text()))
        _restore_daemon_schema(conn)
        conn.execute(
            "INSERT INTO schedules (name, script, command, machine) "
            "VALUES ('resident', 'pass', 'python run.py', 'test')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(cast(LiteralString, _MIGRATION.read_text()))
        assert conn.execute("SELECT name, trigger_kind FROM schedules").fetchall() == [
            ("resident", "daemon")
        ]
