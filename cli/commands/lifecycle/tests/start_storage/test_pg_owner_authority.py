# meiva:note
# The admin-as-owner authority on a real home-owned Postgres.
#
# Every schema-creating path (baseline, checkpoint setup, migrations, the
# pgvector memory table) dials the home's owner-only socket as the OS-user
# administrator acting as the schema owner. These tests prove, against a real
# per-cluster instance with native custody:
#
# - objects come out owner-owned, never admin-owned;
# - the result is privilege-identical to the pre-authority procedure that logged
#   in as the owner (the application roles' surface does not move);
# - re-running provisioning is a no-op;
# - the owner is born NOLOGIN, and migrations, provisioning, `pg_dump` and a
#   restore of that dump keep working without it ever logging in.

from __future__ import annotations

import dataclasses
import getpass
import os
import subprocess
from pathlib import Path

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from base.cluster import (
    ensure_checkpoint_schema,
    ensure_pgvector_extension,
    provision_database,
)
from base.cluster.authority import ensure_groups
from base.cluster.checkpoint_schema import PostgresCheckpointSchema
from base.cluster.dataplane.pg_admin import (
    OwnerAuthority,
    local_owner_authority,
    owner_conninfo,
    pg_admin_url,
)
from base.cluster.dataplane.pg_tools import pg_tool
from base.deploy.schema.migration_layout import MIGRATIONS_DIR
from base.deploy.schema.migrations import apply_pending_migrations, required_migration_set
from base.host.db_authority.model import GATEWAY_GROUP, RUNNER_GROUP, Groups
from cli.commands.data_plane.bringup import prepare_memory_vectors
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.lifecycle.tests.start_storage.owned_postgres import Plane, write_record
from cli.commands.lifecycle.tests.start_storage.owned_postgres import owned_pg as owned_pg
from cli.commands.lifecycle.tests.start_storage.owned_postgres import plane as plane
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.derived.memory_indexer.backends.pgvector import prepare_table
from services.derived.memory_indexer.embeddings.factory import get_descriptor

# meiva:note
# The password is a literal of the throwaway database owner this test creates.
_OWNER_PASSWORD = "owner-login-fixture"  # noqa: S105
_GROUPS = Groups(gateway=GATEWAY_GROUP, runner=RUNNER_GROUP)

# meiva:note
# Every catalog fact an application role's privileges depend on. A NULL ACL
# reads as its built-in default, so an explicit owner-only ACL and an untouched
# one compare equal (pg_dump restores the former as the latter).
_SNAPSHOT: dict[str, sql.SQL] = {
    "relations": sql.SQL(
        "SELECT c.relkind::text, c.relname::text, pg_get_userbyid(c.relowner)::text,"
        " coalesce(c.relacl, acldefault((CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END)::\"char\","
        " c.relowner))::text FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
        " ORDER BY 1, 2"
    ),
    "functions": sql.SQL(
        "SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner)::text,"
        " coalesce(p.proacl, acldefault('f', p.proowner))::text, p.prosecdef::text"
        " FROM pg_proc p"
        " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
        " ORDER BY 1"
    ),
    "types": sql.SQL(
        "SELECT t.typname::text, pg_get_userbyid(t.typowner)::text,"
        " coalesce(t.typacl, acldefault('T', t.typowner))::text"
        " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
        " WHERE n.nspname = 'public' ORDER BY 1"
    ),
    "default_privileges": sql.SQL(
        "SELECT pg_get_userbyid(d.defaclrole)::text, d.defaclobjtype::text,"
        " coalesce(d.defaclnamespace::regnamespace::text, ''), d.defaclacl::text"
        " FROM pg_default_acl d ORDER BY 1, 2, 3"
    ),
    "schema": sql.SQL(
        "SELECT pg_get_userbyid(nspowner)::text, coalesce(nspacl, acldefault('n', nspowner))::text"
        " FROM pg_namespace WHERE nspname = 'public'"
    ),
    "database": sql.SQL(
        "SELECT pg_get_userbyid(datdba)::text, coalesce(datacl, acldefault('d', datdba))::text"
        " FROM pg_database WHERE datname = current_database()"
    ),
    "runner_effective": sql.SQL(
        "SELECT c.relname::text, p.privilege FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',"
        " 'REFERENCES', 'TRIGGER']) AS p(privilege)"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')"
        " AND has_table_privilege('meiva_runner', c.oid, p.privilege) ORDER BY 1, 2"
    ),
}

# meiva:note
# Public objects that are not extension members and not owned by `owner`.
_FOREIGN_OWNED = sql.SQL(
    "SELECT kind, name, owner FROM ("
    " SELECT 'relation' AS kind, c.relname::text AS name,"
    "  pg_get_userbyid(c.relowner)::text AS owner, 'pg_class'::regclass AS cls, c.oid"
    "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'"
    " UNION ALL"
    " SELECT 'function', p.oid::regprocedure::text, pg_get_userbyid(p.proowner)::text,"
    "  'pg_proc'::regclass, p.oid"
    "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'"
    " UNION ALL"
    " SELECT 'type', t.typname::text, pg_get_userbyid(t.typowner)::text, 'pg_type'::regclass,"
    "  t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
    "  WHERE n.nspname = 'public'"
    ") o WHERE owner <> %s AND NOT EXISTS ("
    " SELECT 1 FROM pg_depend d WHERE d.classid = o.cls AND d.objid = o.oid"
    " AND d.deptype = 'e') ORDER BY 1, 2"
)


def _vector_available(plane: Plane) -> bool:
    with psycopg.connect(plane.admin_on("postgres")) as conn:
        row = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        return row.fetchone() is not None


# meiva:note
# The owner's name normalized; an ACL array's items sorted (their order is
# grant order — the capability groups are cluster-wide, so a second database
# sees them before its schema grants run).
def _normalized(value: object, owner: str) -> str:
    text = str(value).replace(owner, "<owner>")
    if text.startswith("{") and text.endswith("}") and "=" in text:
        return "{" + ",".join(sorted(text[1:-1].split(","))) + "}"
    return text


# meiva:note
# Every privilege-bearing catalog fact, with the owner's name normalized.
def _snapshot(conninfo: str, owner: str) -> dict[str, list[tuple[str, ...]]]:
    with psycopg.connect(conninfo) as conn:
        return {
            name: [
                tuple(_normalized(value, owner) for value in row)
                for row in conn.execute(query).fetchall()
            ]
            for name, query in _SNAPSHOT.items()
        }


def _foreign_owned(conninfo: str, owner: str) -> list[tuple[str, str, str]]:
    with psycopg.connect(conninfo) as conn:
        return [(str(a), str(b), str(c)) for a, b, c in conn.execute(_FOREIGN_OWNED, (owner,))]


# meiva:note
# (applied migration names, whether the pgvector memory table exists).
def _prepared_state(conninfo: str) -> tuple[set[str], bool]:
    with psycopg.connect(conninfo) as conn:
        applied = {str(row[0]) for row in conn.execute("SELECT name FROM schema_migrations")}
        memory = conn.execute("SELECT to_regclass('public.memory_embeddings')").fetchone()
    return applied, memory == ("memory_embeddings",)


# meiva:note
# The procedure before the admin authority: DDL by logging in as the owner.
#
# Role, database, extension and group grants were already admin work; the
# schema baseline, checkpoint setup, migrations and the indexer's memory
# table ran over the owner's own login (a LOGIN owner with a password).
def _provision_by_owner_login(plane: Plane, identity: str) -> None:
    admin = pg_admin_url(plane.port)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
                sql.Identifier(identity), sql.Literal(_OWNER_PASSWORD)
            )
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(identity), sql.Identifier(identity)
            )
        )
    login = f"postgresql://{identity}:{_OWNER_PASSWORD}@127.0.0.1:{plane.port}/{identity}"
    schema = (Path(__file__).resolve().parents[5] / "db" / "schema.sql").read_text()
    with psycopg.connect(login, autocommit=True) as conn:
        # meiva:note
        # The schema is read from disk at run time, so it is sent as bytes: psycopg types a
        # literal-string query statically.
        conn.execute(schema.encode())
    ensure_pgvector_extension(identity, base_admin_url=admin)
    with PostgresSaver.from_conn_string(login) as saver:
        saver.setup()
    with psycopg.connect(login) as conn:
        apply_pending_migrations(
            conn,
            machine_name=lambda: "owner-procedure-test",
            migrations_dir=MIGRATIONS_DIR,
            home=plane.home,
        )
    _grant_groups(plane, identity)
    with psycopg.connect(login) as conn:
        prepare_table(conn, get_descriptor(plane.config.embedding_backend).dim)


def _grant_groups(plane: Plane, identity: str) -> None:
    with psycopg.connect(plane.admin_on(identity), autocommit=True) as conn:
        ensure_groups(conn, owner=identity, database=identity, groups=_GROUPS)


# meiva:note
# Today's start order through the admin authority; returns database creation.
def _provision_by_authority(plane: Plane, identity: str) -> bool:
    admin = pg_admin_url(plane.port)
    created = provision_database(identity, base_admin_url=admin, expected_data_dir=plane.data_dir)
    _prepare_by_authority(plane, identity, database_created=created)
    return created


def _prepare_by_authority(plane: Plane, identity: str, *, database_created: bool) -> None:
    admin = pg_admin_url(plane.port)
    ensure_pgvector_extension(identity, base_admin_url=admin, expected_data_dir=plane.data_dir)

    ensure_checkpoint_schema(
        identity,
        base_admin_url=admin,
        database_created=database_created,
        expected_data_dir=plane.data_dir,
        schema=PostgresCheckpointSchema(),
    )
    plane.migrate(identity)
    ensure_pgvector_extension(identity, base_admin_url=admin, expected_data_dir=plane.data_dir)
    prepare_memory_vectors(plane.config_for(identity), database_factory=plane.database)
    _grant_groups(plane, identity)


# meiva:note
# Same catalog, same application privileges, only the dial changed.
def test_authority_matches_owner_login_privileges_and_is_idempotent(plane: Plane) -> None:
    legacy, current = "meiva_lgcy", "meiva_auth"
    _provision_by_owner_login(plane, legacy)

    assert _provision_by_authority(plane, current) is True

    after_birth = _snapshot(plane.admin_on(current), current)
    assert after_birth == _snapshot(plane.admin_on(legacy), legacy)
    assert _foreign_owned(plane.admin_on(current), current) == []
    assert _prepared_state(plane.admin_on(current)) == (
        required_migration_set(migrations_dir=MIGRATIONS_DIR),
        _vector_available(plane),
    )

    # meiva:note
    # A second start over the same home changes nothing.
    assert _provision_by_authority(plane, current) is False
    assert plane.migrate(current) == []
    assert _snapshot(plane.admin_on(current), current) == after_birth


# meiva:note
# The owner is born NOLOGIN; it still migrates, provisions and dumps.
def test_owner_without_login_keeps_every_admin_path(plane: Plane, tmp_path: Path) -> None:
    identity = "meiva_nolg"
    admin = pg_admin_url(plane.port)
    created = provision_database(identity, base_admin_url=admin, expected_data_dir=plane.data_dir)
    with psycopg.connect(admin, autocommit=True) as conn:
        assert conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL FROM pg_authid WHERE rolname = %s",
            (identity,),
        ).fetchone() == (False, True)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(
            f"postgresql://{identity}:{_OWNER_PASSWORD}@127.0.0.1:{plane.port}/{identity}"
        )

    _prepare_by_authority(plane, identity, database_created=created)

    authority = local_owner_authority(plane.url(identity), remote_plane=False)
    assert authority == OwnerAuthority(
        admin_url=admin, database=identity, owner=identity, data_dir=plane.data_dir
    )
    with authority.session() as conn:
        facts = conn.execute(
            "SELECT session_user, current_user, current_setting('statement_timeout')"
        ).fetchone()
    assert facts == (getpass.getuser(), identity, "0")
    assert _foreign_owned(authority.conninfo, identity) == []
    source = _snapshot(plane.admin_on(identity), identity)

    # meiva:note
    # pg_dump acts as the owner with no password in argv or the environment.
    dump = tmp_path / "owner.dump"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    subprocess.run(
        [
            str(pg_tool("pg_dump")),
            "--format=custom",
            "--file",
            str(dump),
            "--dbname",
            authority.conninfo,
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    restored = f"{identity}_rst"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(restored), sql.Identifier(identity)
            )
        )
    subprocess.run(
        [
            str(pg_tool("pg_restore")),
            "--clean",
            "--if-exists",
            "--exit-on-error",
            "--dbname",
            plane.admin_on(restored),
            str(dump),
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    assert _foreign_owned(plane.admin_on(restored), identity) == []
    copy = _snapshot(plane.admin_on(restored), identity)
    assert {name: rows for name, rows in copy.items() if name != "database"} == {
        name: rows for name, rows in source.items() if name != "database"
    }


# meiva:note
# The owner socket is dialed under a custody check: a backend that is not the home's own
# postmaster (here, one claiming a different data directory) is refused before the caller runs
# a statement.
def test_an_authority_bound_to_another_postgres_refuses_before_any_ddl(plane: Plane) -> None:
    identity = "meiva_cust"
    created = provision_database(
        identity, base_admin_url=pg_admin_url(plane.port), expected_data_dir=plane.data_dir
    )
    assert created is True
    authority = local_owner_authority(plane.url(identity), remote_plane=False)
    foreign = dataclasses.replace(authority, data_dir=plane.home / "another-pg")

    with authority.session() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    with pytest.raises(RuntimeError), foreign.session():
        pytest.fail("a session on a foreign backend must not open")


# meiva:note
# The owner travels as `-c role=<owner>`; anything else is refused.
def test_owner_conninfo_refuses_what_startup_options_cannot_carry() -> None:
    admin = "postgresql://admin@/postgres?host=/sockets/meiva-pg-x&port=5433"
    with pytest.raises(ValueError, match="plain role identifier"):
        owner_conninfo(admin, database="meiva", owner="meiva owner")
    with pytest.raises(ValueError, match="startup options"):
        owner_conninfo(f"{admin}&options=-c%20role%3Dother", database="meiva", owner="meiva")
    assert conninfo_to_dict(owner_conninfo(admin, database="meiva_main", owner="meiva_main")) == {
        "user": "admin",
        "dbname": "meiva_main",
        "host": "/sockets/meiva-pg-x",
        "port": "5433",
        "options": "-c role=meiva_main",
    }


# meiva:note
# Socket port from the cluster record; owner and database from the URL's database name.
def test_local_owner_authority_reads_record_and_url_as_data(unit_home: Path) -> None:
    write_record(unit_home, {"postgres": 5999, "pgbouncer": 6999})
    # meiva:note
    # A delivered generation login replaces the URL's username; the owner is the
    # database's same-named identity.
    authority = local_owner_authority(
        "postgresql://meiva_g2_gateway:pw@127.0.0.1:6999/meiva_db", remote_plane=False
    )

    assert (authority.database, authority.owner) == ("meiva_db", "meiva_db")
    assert authority.data_dir == unit_home / "pg"
    assert conninfo_to_dict(authority.admin_url)["port"] == "5999"
    assert "pw" not in authority.conninfo


def test_remote_plane_has_no_local_owner_authority() -> None:
    with pytest.raises(RuntimeError, match="remote-managed"):
        local_owner_authority("postgresql://owner@db.example/meiva", remote_plane=True)
