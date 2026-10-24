from __future__ import annotations

import os
import time
from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

from base.config import settings

_SCHEMA_SQL = Path(__file__).resolve().parents[4] / "db" / "schema.sql"


def _throwaway_database(name: str) -> str:
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    admin_url = f"{base_url}/postgres"
    database = f"meiva_test_{name}_{os.getpid()}_{int(time.time() * 1_000_000)}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    return f"{base_url}/{database}"


def _drop_database(url: str) -> None:
    base_url, database = url.rsplit("/", 1)
    with psycopg.connect(f"{base_url}/postgres", autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))


def test_the_exit_status_pair_exists_and_its_writer_compiles(db_url: str) -> None:
    del db_url
    url = _throwaway_database("harness_exit")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(cast(LiteralString, _SCHEMA_SQL.read_text()))
            columns = dict(
                conn.execute(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'agents_meta' "
                    "AND column_name IN ('harness_exit_code', 'harness_exit_at')"
                ).fetchall()
            )
            assert columns == {"harness_exit_code": "YES", "harness_exit_at": "YES"}
            written = conn.execute(
                "UPDATE agents_meta SET harness_exit_code = %s, harness_exit_at = now() "
                "WHERE id = %s",
                (7, -1),
            )
            assert written.rowcount == 0
    finally:
        _drop_database(url)
