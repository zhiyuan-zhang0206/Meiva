from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from base.agents import impersonation as leases
from base.agents.impersonation import INSERT_LEASE_SQL, RENEW_LEASE_SQL
from base.cluster.authority import ensure_groups
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.deploy.tests.db_authority_tests import AuthorityCluster
from base.events.live.bus import EventBus
from base.host.net.url_secret import url_with_userinfo
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths.tests.homes import unit_home as unit_home


def _open_lease(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...] | None:
    return conn.execute(
        "INSERT INTO agent_impersonations (id, agent_id, source, machine, status,"
        " ttl_seconds, expires_at) VALUES (gen_random_uuid(), %s, 'codex', 'test-machine',"
        " 'requested', 3600, now() + interval '1 hour') RETURNING session_id",
        (agent_id,),
    ).fetchone()


def test_runner_allocates_impersonation_sessions_without_agents_update(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        row = conn.execute("INSERT INTO agents (label) VALUES ('seed') RETURNING id").fetchone()
        assert row is not None
        agent_id = row[0]
        conn.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'idling')",
            (agent_id,),
        )

    with cluster.connect_class("runner", autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE agents SET label = 'denied' WHERE id = %s", (agent_id,))
        assert _open_lease(conn, agent_id) == (0,)
        assert conn.execute(
            "SELECT impersonation_index FROM agents WHERE id = %s", (agent_id,)
        ).fetchone() == (1,)


def test_runner_transport_maintenance_grants_do_not_allow_destination_mutation(
    authority_postgres: AuthorityCluster,
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        row = conn.execute(
            "INSERT INTO agents(label) VALUES('transport grants') RETURNING id"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO agents_meta(id,spawner,status) VALUES(%s,'user','idling')", (row[0],)
        )
        assert _open_lease(conn, row[0]) == (0,)
        ensure_groups(conn, owner=cluster.owner, database=cluster.database, groups=cluster.groups)
    with cluster.connect_class("runner", autocommit=True) as conn:
        conn.execute(
            "UPDATE agent_impersonations SET relay_generation=1,"
            "relay_degraded_reason='unknown' WHERE agent_id=%s",
            (row[0],),
        )
        for column in (
            "terminal_notice_snapshot",
            "terminal_notice_pending_at",
            "terminal_notice_attempts",
            "relay_thread_id",
            "machine",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    sql.SQL(
                        "UPDATE agent_impersonations SET {column}=NULL WHERE agent_id=%s"
                    ).format(column=sql.Identifier(column)),
                    (row[0],),
                )


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LOCAL_MACHINE = "test-machine"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
_BASELINE_GRANT = re.compile(
    r"GRANT\s+(INSERT|UPDATE)\s*\(([^)]*)\)\s*ON\s+agent_impersonations\s+TO\s+meiva_runner"
)


# meiva:note
# The lease lifecycle wakes the agent through Redis. These tests own a database,
# not a broker, and a broker is not what #5375 broke.
def _no_wake(*_args: object, **_kwargs: object) -> None: ...


def _assigned_columns(assignments: str) -> set[str]:
    columns: set[str] = set()
    depth = 0
    start = 0
    for index, char in enumerate(assignments + ","):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            columns.add(assignments[start:index].split("=", 1)[0].strip())
            start = index + 1
    return columns


# meiva:note
# Read the columns off the statements the runner executes instead of restating
# them in a list: a copy drifts from its statement exactly the way the grants
# drifted here.
def _lease_statement_columns() -> dict[str, set[str]]:
    insert = INSERT_LEASE_SQL.split("(", 1)[1].split(")", 1)[0]
    renew = RENEW_LEASE_SQL.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    columns = {
        "INSERT": {column.strip() for column in insert.split(",")},
        "UPDATE": _assigned_columns(renew),
    }
    for privilege, named in columns.items():
        assert named, f"{privilege} statement named no columns — the statement parse broke"
        assert all(_IDENTIFIER.match(column) for column in named), (
            f"{privilege} parse produced non-columns: {sorted(named)}"
        )
    return columns


def _assert_covered(granted: dict[str, set[str]], *, source: str) -> None:
    for privilege, named in _lease_statement_columns().items():
        missing = sorted(named - granted.get(privilege, set()))
        assert not missing, (
            f"{source} never grants meiva_runner {privilege} on agent_impersonations columns"
            f" {missing}"
        )


def _live_column_grants(conn: psycopg.Connection) -> dict[str, set[str]]:
    granted: dict[str, set[str]] = {}
    for privilege, column in conn.execute(
        "SELECT privilege_type, column_name FROM information_schema.column_privileges"
        " WHERE table_schema='public' AND table_name='agent_impersonations'"
    ).fetchall():
        granted.setdefault(privilege, set()).add(column)
    return granted


def _baseline_column_grants() -> dict[str, set[str]]:
    baseline = (_REPO_ROOT / "db" / "schema.sql").read_text()
    granted: dict[str, set[str]] = {}
    for privilege, columns in _BASELINE_GRANT.findall(baseline):
        granted.setdefault(privilege, set()).update(
            column.strip() for column in columns.split(",") if column.strip()
        )
    return granted


# meiva:note
# Spawning is the gateway's job, never the runner's, so the agent and its native
# owner are seeded as the admin; only the lease lifecycle below runs as the
# runner login.
def _seed_lease_agent(conn: psycopg.Connection) -> tuple[int, RuntimeIncarnation]:
    row = conn.execute("INSERT INTO agents (label) VALUES ('lease grants') RETURNING id").fetchone()
    assert row is not None
    agent_id = int(row[0])
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, machine, runtime_generation,"
        " runtime_owner, runtime_kind, lease_expires_at)"
        " VALUES (%s, 'user', 'idling', %s, %s, %s, 'process',"
        " clock_timestamp() + interval '10 minutes')",
        (agent_id, _LOCAL_MACHINE, incarnation.generation, incarnation.owner),
    )
    return agent_id, incarnation


# meiva:note
# The lease calls below dial through a `Database` handle because that is the
# agent CLI's shape; the handle carries the born runner login so the grant check
# runs with the runner's privileges, not the admin's.
def _runner_database(cluster: AuthorityCluster) -> Database:
    role = cluster.active_secret().roles.of("runner")
    url = url_with_userinfo(
        f"postgresql://127.0.0.1:{cluster.instance.port}/{cluster.database}",
        role.name,
        role.password,
    )
    return Database(
        DbConfig(
            db_url=url,
            db_sslmode="",
            db_pool_min_size=1,
            db_pool_max_size=2,
            pgbouncer_enabled=False,
        ),
        gate=ProcessDbGate(version=lambda: 0, process="runner-grants", exempt=True),
    )


# meiva:note
# The runner holds `agent_impersonations` by column, never by table, so a column
# added to a lease statement without a grant dies at runtime with
# InsufficientPrivilege. Task #5375 was exactly that: the capability columns
# reached the request INSERT and the renewal UPDATE without reaching the
# birth-time convergence, and `meiva impersonation request`/`renew` broke on
# runner-login agents while the gateway's table grant kept its own leases
# working. Reading the columns off the statements covers the next added column
# without anyone restating a list.
def test_runner_column_grants_cover_the_lease_statements(
    authority_postgres: AuthorityCluster,
) -> None:
    with authority_postgres.connect_class("runner") as conn:
        _assert_covered(_live_column_grants(conn), source="the birth-time grant convergence")


# meiva:note
# `db/schema.sql` is the second copy of that grant list, and a fresh install
# creates the group roles first (`scripts/ci/test_migrations_apply.sh` does), so
# the baseline GRANT blocks run for real. A column added to a statement has to
# reach this copy too, or a new cluster ships the same bug.
def test_baseline_schema_grants_cover_the_lease_statements() -> None:
    granted = _baseline_column_grants()
    assert granted, "db/schema.sql no longer declares the runner's column grants"
    _assert_covered(granted, source="db/schema.sql")


# meiva:note
# request -> accept -> activate -> renew -> release, executed by the runner login
# itself. This is the production shape #5375 broke: a column written by the request
# INSERT or the renewal UPDATE and missing from the runner's column-level grant
# dies here, while the gateway's table-level grant keeps its own leases working.
# The caller vocabulary comes from the impersonation package because `base.deploy`
# must not reach `base.api_contracts`.
def test_runner_drives_the_lease_lifecycle(
    authority_postgres: AuthorityCluster, config_authority: ConfigAuthority
) -> None:
    cluster = authority_postgres
    with cluster.admin() as conn:
        agent_id, incarnation = _seed_lease_agent(conn)
    database = _runner_database(cluster)
    bus = EventBus.from_settings()
    with pytest.MonkeyPatch.context() as silent_wake:
        silent_wake.setattr(leases, "wake_agent", _no_wake)
        lease = leases.request(
            database,
            bus,
            agent_id,
            caller=leases.CallerIdentity(kind="external_agent", subject="codex"),
            ttl_seconds=300,
            reason="runner grant matrix",
            relay_provider="codex",
            relay_thread_id=str(uuid4()),
            authority=config_authority,
            local_machine=_LOCAL_MACHINE,
        )
        leases.accept(
            database,
            bus,
            lease["id"],
            agent_id,
            incarnation,
            "Grant-matrix handoff",
            local_machine=_LOCAL_MACHINE,
        )
        leases.activate(database, bus, lease["id"], incarnation)
        renewed = leases.renew(
            database, bus, lease["id"], ttl_seconds=600, local_machine=_LOCAL_MACHINE
        )
        assert renewed["ttl_seconds"] == 600
        released = leases.release(
            database, bus, lease["id"], "Grant-matrix release", local_machine=_LOCAL_MACHINE
        )
        assert released["status"] == "released"
