from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from base.cluster import drop_database, provision_database
from base.cluster.authority.provision_plan import (
    CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS,
    expected_checkpoint_schema_versions,
    plan_checkpoint_schema,
    plan_pgvector_extension,
)
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane.pg_admin import owner_conninfo
from base.cluster.derive import swap_db
from base.cluster.provision import ensure_checkpoint_schema, ensure_pgvector_extension
from base.config import settings


def _admin_url() -> str:
    base, _ = settings.data_plane.db_url.rsplit("/", 1)
    return f"{base}/postgres"


def _fresh_identity() -> str:
    return f"meiva_provtest_{os.getpid()}_{int(time.time() * 1_000_000)}"


def test_provision_database_creates_db_and_applies_schema(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()
    base_url, _ = settings.data_plane.db_url.rsplit("/", 1)
    db_url = f"{base_url}/{expected_db}"

    try:
        assert provision_database(identity, base_admin_url=admin_url) is True

        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None, f"database {expected_db!r} was not created"

        with psycopg.connect(db_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT EXISTS ("
                "  SELECT 1 FROM information_schema.tables"
                "  WHERE table_schema = 'public' AND table_name = 'agents'"
                ")"
            ).fetchone()
        assert row is not None and row[0] is True, "table 'agents' not found — schema not applied"

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_drop_database_removes_db_idempotent(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    provision_database(identity, base_admin_url=admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is not None
        )

    drop_database(identity, base_admin_url=admin_url)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        assert (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)).fetchone()
            is None
        ), f"database {expected_db!r} still present after drop_database"

    drop_database(identity, base_admin_url=admin_url)


def test_provision_database_idempotent(db_conn: object) -> None:
    identity = _fresh_identity()
    expected_db = identity
    admin_url = _admin_url()

    try:
        assert provision_database(identity, base_admin_url=admin_url) is True
        assert provision_database(identity, base_admin_url=admin_url) is False

        with psycopg.connect(admin_url, autocommit=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
        assert row is not None

    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = %s AND pid <> pg_backend_pid()",
                (expected_db,),
            )
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(expected_db)))
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(expected_db)))


def test_provisioned_role_is_nosuperuser_and_owns_db(db_conn: object) -> None:
    role = expected_db = _fresh_identity()
    admin_url = _admin_url()
    try:
        provision_database(role, base_admin_url=admin_url)
        with psycopg.connect(admin_url, autocommit=True) as conn:
            attrs = conn.execute(
                "SELECT rolsuper, rolcanlogin, rolpassword IS NULL FROM pg_authid"
                " WHERE rolname = %s",
                (role,),
            ).fetchone()
            assert attrs == (False, False, True), "role must be NOLOGIN NOSUPERUSER, no password"
            owner = conn.execute(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (expected_db,)
            ).fetchone()
            assert owner is not None and owner[0] == role, "role must own its own database"
    finally:
        _drop_db_and_role(admin_url, expected_db)


def test_role_cannot_read_another_clusters_database(db_conn: object) -> None:
    base = _fresh_identity()
    a, b = f"{base}a", f"{base}b"
    admin_url = _admin_url()
    db_a, role_b = a, b
    try:
        provision_database(a, base_admin_url=admin_url)
        provision_database(b, base_admin_url=admin_url)
        url_a_as_b = owner_conninfo(admin_url, database=db_a, owner=role_b)
        with (
            psycopg.connect(url_a_as_b, autocommit=True) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute("SELECT * FROM agents")
    finally:
        _drop_db_and_role(admin_url, a)
        _drop_db_and_role(admin_url, b)


def _drop_db_and_role(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_ensure_pgvector_extension_noop_when_binaries_absent() -> None:
    assert plan_pgvector_extension(extension_available=False) is None


def test_ensure_pgvector_extension_precreates_in_cluster_db(db_conn: object) -> None:
    assert (
        plan_pgvector_extension(extension_available=True) == "CREATE EXTENSION IF NOT EXISTS vector"
    )
    identity = _fresh_identity()
    admin_url = _admin_url()
    try:
        provision_database(identity, base_admin_url=admin_url)
        ensure_pgvector_extension(identity, base_admin_url=admin_url)
    finally:
        _drop_db_and_role(admin_url, identity)


def test_ensure_pgvector_extension_noop_on_connect_failure(
    tmp_path: Path,
    postgres_access: None,
    loguru_records: list[dict[str, Any]],
) -> None:
    dead_socket_url = f"postgresql://admin@/postgres?host={tmp_path}"
    ensure_pgvector_extension("meiva_ident", base_admin_url=dead_socket_url)
    [warning] = [r for r in loguru_records if "[pgvector] pre-create skipped" in r["message"]]
    assert "cluster Postgres unreachable over the admin connection" in warning["message"]


def test_interrupted_empty_database_requires_initialization_authority(db_conn: object) -> None:
    identity = _fresh_identity()
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    try:
        with pytest.raises(RuntimeError, match="without initialization authority"):
            provision_database(identity, base_admin_url=admin_url)
        assert (
            provision_database(
                identity,
                base_admin_url=admin_url,
                resume_initialization=True,
            )
            is False
        )
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            assert conn.execute("SELECT to_regclass('public.agents')").fetchone() == ("agents",)
    finally:
        _drop_db_and_role(admin_url, identity)


def test_interrupted_database_with_unknown_objects_is_preserved(db_conn: object) -> None:
    identity = _fresh_identity()
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(identity)))
    try:
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            conn.execute("CREATE TABLE preserve_me (value text)")
            conn.execute("INSERT INTO preserve_me VALUES ('user data')")
        with pytest.raises(RuntimeError, match="unknown database objects"):
            provision_database(
                identity,
                base_admin_url=admin_url,
                resume_initialization=True,
            )
        with psycopg.connect(swap_db(admin_url, identity)) as conn:
            assert conn.execute("SELECT value FROM preserve_me").fetchall() == [("user data",)]
            assert conn.execute("SELECT to_regclass('public.schema_migrations')").fetchone() == (
                None,
            )
    finally:
        _drop_db_and_role(admin_url, identity)


class _RefusingSchema:
    @property
    def migrations(self) -> tuple[str, ...]:
        return tuple(PostgresCheckpointSchema().migrations)

    def setup(
        self,
        *,
        base_admin_url: str,
        identity: str,
        expected_data_dir: Path | None = None,
    ) -> None:
        raise AssertionError("setup must not run when schema is already current")


def test_default_schema_check_never_setup_after_concurrent_repair(db_conn: object) -> None:
    expected = expected_checkpoint_schema_versions(
        PostgresCheckpointSchema().migrations, CHECKPOINT_SCHEMA_MEIVA_MIGRATIONS
    )
    assert (
        plan_checkpoint_schema(
            expected - {9}, expected, database_created=False, resume_partial=False
        )
        == "verify_only"
    )
    assert (
        plan_checkpoint_schema(expected, expected, database_created=False, resume_partial=False)
        == "noop"
    )
    identity = _fresh_identity()
    admin_url = _admin_url()
    try:
        provision_database(identity, base_admin_url=admin_url)
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin_url,
            schema=PostgresCheckpointSchema(),
            database_created=True,
        )
        ensure_checkpoint_schema(
            identity,
            base_admin_url=admin_url,
            schema=_RefusingSchema(),
        )
    finally:
        _drop_db_and_role(admin_url, identity)
