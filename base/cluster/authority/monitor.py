"""The stable monitoring login: the OTel collector's PostgreSQL receiver.

``meiva_monitor`` is not a write generation. It holds no application privilege
and is delivered no credential. It is created without any password, and ``pg_hba`` admits it
only by ``peer`` on the home's owner-only socket, mapped from the home's OS
user (``pg_ident`` map ``meiva_monitor``); SCRAM can never authenticate a role
without a verifier.
No credential therefore exists to store in the collector configuration.

Its whole privilege surface, verified by the invariant:

- ``LOGIN INHERIT``, nothing elevated, no settings, no limit or expiry, no
  password; owns nothing and has no members;
- exactly one membership, ``pg_read_all_stats`` (``INHERIT TRUE, SET FALSE``):
  other sessions' ``pg_stat_activity``/``pg_stat_replication`` rows and every
  database's size;
- exactly one direct grant, ``CONNECT`` on the cluster database (PUBLIC lost
  it). Statistics views and ``pg_relation_size`` need no table privilege, so
  it reads no application row.
"""

from __future__ import annotations

from psycopg import sql

from base.cluster.authority.catalog import (
    Conn,
    memberships,
    require_admin,
    role_facts,
)
from base.cluster.authority.checks import (
    MONITOR_MAP,
    MONITOR_ROLE,
    check_monitor_facts,
)
from base.cluster.authority.checks import (
    STATS_ROLE as _STATS_ROLE,
)
from base.host.db_authority.model import AuthorityRefusedError, CatalogRefusedError

__all__ = ["MONITOR_MAP", "MONITOR_ROLE", "ensure_monitor", "monitor_violations"]

_DIRECT_GRANTS = """
SELECT d.dbid, d.classid::regclass::text, d.objid, d.deptype
FROM pg_shdepend d JOIN pg_authid a ON a.oid = d.refobjid
WHERE d.refclassid = 'pg_authid'::regclass AND a.rolname = %s
ORDER BY 1, 2, 3, 4
"""


def monitor_violations(conn: Conn, *, database: str) -> list[str]:
    """Why the existing monitor role is not exactly its shape (absent: none)."""
    fact = role_facts(conn, (MONITOR_ROLE,)).get(MONITOR_ROLE)
    if fact is None:
        return []
    rows = memberships(conn, (MONITOR_ROLE,))
    row = conn.execute("SELECT oid FROM pg_database WHERE datname = %s", (database,)).fetchone()
    grants = [tuple(grant) for grant in conn.execute(_DIRECT_GRANTS, (MONITOR_ROLE,)).fetchall()]
    return check_monitor_facts(
        fact,
        database=database,
        database_oid=None if row is None else row[0],
        memberships=rows,
        grants=grants,
    )


def ensure_monitor(conn: Conn, *, database: str) -> None:
    """Create the monitor login if missing and converge its two grants.

    Idempotent. An existing role of any other shape is an unknown effect and
    refuses; a missing one is created password-less.
    """
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("the monitor role must converge in the cluster database")
    role = sql.Identifier(MONITOR_ROLE)
    with conn.transaction():
        if MONITOR_ROLE not in role_facts(conn, (MONITOR_ROLE,)):
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE"
                    " NOREPLICATION NOBYPASSRLS PASSWORD NULL"
                ).format(role)
            )
        conn.execute(
            sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE").format(
                sql.Identifier(_STATS_ROLE), role
            )
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role)
        )
        violations = monitor_violations(conn, database=database)
        if violations:
            raise CatalogRefusedError(tuple(violations))
