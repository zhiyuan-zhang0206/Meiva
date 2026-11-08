"""Group grants and the fail-closed invariant on real PostgreSQL 17, and
MAINTAIN/VACUUM. The runner matrix itself is exercised through a group login in
base/agents/incarnation/tests/test_runner_role.py."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from base.cluster.authority import (
    VacuumSkippedError,
    check_invariant,
    ensure_groups,
    ensure_monitor,
    vacuum_or_fail,
)
from base.deploy.tests.db_authority_tests import OWNER, AuthorityCluster
from base.host.db_authority.model import CatalogRefusedError

# (mutation run by the admin, expected violation fragment)
_UNKNOWN_EFFECTS = [
    ("ALTER ROLE meiva_g0_gateway CREATEDB", "meiva_g0_gateway holds CREATEDB"),
    (f"GRANT {OWNER} TO meiva_g0_runner", "schema-owner membership"),
    ("ALTER ROLE meiva_g0_gateway SET work_mem = '8MB'", "meiva_g0_gateway carries role settings"),
    (
        "REVOKE meiva_gateway FROM meiva_g0_gateway;"
        " GRANT meiva_gateway TO meiva_g0_gateway WITH INHERIT TRUE, SET TRUE, ADMIN FALSE",
        "differ from INHERIT-only",
    ),
    ("ALTER ROLE meiva_g0_gateway PASSWORD 'foreign-credential'", "stored verifier differs"),
    ("ALTER ROLE meiva_gateway LOGIN", "group meiva_gateway can log in"),
    (f"ALTER ROLE {OWNER} LOGIN", "schema owner meiva_test_owner can log in"),
    ("CREATE ROLE stray LOGIN PASSWORD 'stray-password'; GRANT meiva_runner TO stray", "stray"),
    ("CREATE ROLE aux LOGIN; GRANT pg_write_all_data TO aux", "login aux holds write privileges"),
    ("CREATE ROLE su LOGIN SUPERUSER", "login su is an unexpected superuser"),
    ("DROP ROLE meiva_g0_runner", "meiva_g0_runner does not exist"),
]


@pytest.mark.parametrize(("mutation", "violation"), _UNKNOWN_EFFECTS)
def test_invariant_refuses_every_unknown_effect(
    authority_postgres: AuthorityCluster, mutation: str, violation: str
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute(mutation.format(db=cluster.database))  # type: ignore[arg-type]
        with pytest.raises(CatalogRefusedError) as refused:
            check_invariant(conn, cluster.home, database=cluster.database)
    assert any(violation in item for item in refused.value.violations), refused.value.violations


# (drift of the converged monitoring login, expected violation fragment)
_MONITOR_DRIFT = [
    ("ALTER ROLE meiva_monitor PASSWORD 'monitor-password'", "meiva_monitor has a password"),
    ("ALTER ROLE meiva_monitor CREATEROLE", "meiva_monitor holds attributes"),
    ("ALTER ROLE meiva_monitor SET work_mem = '8MB'", "meiva_monitor holds attributes"),
    ("GRANT pg_monitor TO meiva_monitor", "meiva_monitor memberships"),
    ("CREATE ROLE aux LOGIN; GRANT meiva_monitor TO aux", "meiva_monitor has members"),
]


@pytest.mark.parametrize(("mutation", "violation"), _MONITOR_DRIFT)
def test_invariant_holds_the_monitoring_login_to_its_shape(
    authority_postgres: AuthorityCluster, mutation: str, violation: str
) -> None:
    """The converged monitor (password-less LOGIN, INHERIT-only pg_read_all_stats,
    CONNECT on the cluster database) passes the invariant; any drift is a
    violation, and converging again refuses instead of repairing it."""
    cluster = authority_postgres
    with cluster.admin() as conn:
        ensure_monitor(conn, database=cluster.database)
        ensure_monitor(conn, database=cluster.database)
        facts = conn.execute(
            "SELECT rolcanlogin, rolpassword IS NULL, rolsuper FROM pg_authid"
            " WHERE rolname = 'meiva_monitor'"
        ).fetchone()
        assert facts == (True, True, False)
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute(mutation)  # type: ignore[arg-type]
        with pytest.raises(CatalogRefusedError) as refused:
            check_invariant(conn, cluster.home, database=cluster.database)
        assert any(violation in item for item in refused.value.violations), refused.value.violations
        with pytest.raises(CatalogRefusedError):
            ensure_monitor(conn, database=cluster.database)


def test_default_privileges_cover_tables_created_after_birth(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(OWNER)))
        conn.execute("CREATE TABLE later_migration (id bigserial PRIMARY KEY, note text)")
        conn.execute("RESET ROLE")
        check_invariant(conn, cluster.home, database=cluster.database)
    with cluster.connect_class("gateway", autocommit=True) as gateway:
        gateway.execute("INSERT INTO later_migration (note) VALUES ('gateway')")
        gateway.execute("UPDATE later_migration SET note = 'updated'")
    with cluster.connect_class("runner", autocommit=True) as runner:
        assert runner.execute("SELECT note FROM later_migration").fetchall() == [("updated",)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("INSERT INTO later_migration (note) VALUES ('runner')")


def test_ensure_groups_is_idempotent_and_never_changes_login(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        for _ in range(2):
            ensure_groups(
                conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
            )
        check_invariant(conn, cluster.home, database=cluster.database)
        conn.execute("ALTER ROLE meiva_runner LOGIN")
        with pytest.raises(CatalogRefusedError, match="group meiva_runner can log in"):
            ensure_groups(
                conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups
            )
        row = conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'meiva_runner'"
        ).fetchone()
        assert row == (True,)


def test_gateway_vacuum_requires_maintain_and_a_skip_is_a_failure(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.connect_class("gateway", autocommit=True) as gateway:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            vacuum_or_fail(gateway, table)
        with cluster.admin() as conn:
            conn.execute("REVOKE MAINTAIN ON checkpoints FROM meiva_gateway")
        notices: list[str] = []
        gateway.add_notice_handler(lambda diag: notices.append(diag.message_primary or ""))
        gateway.execute("VACUUM (ANALYZE) checkpoints")  # plain VACUUM only warns
        assert any("skipping" in notice for notice in notices)
        with pytest.raises(VacuumSkippedError, match="permission denied to vacuum"):
            vacuum_or_fail(gateway, "checkpoints")
