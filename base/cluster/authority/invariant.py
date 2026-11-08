# meiva:note
# The read-only authority invariant: the catalog must equal the ledger.
#
# Fail closed. Every unknown effect is a violation, and all violations are
# reported together in one `CatalogRefusedError`:
#
# - the owner is NOLOGIN, passwordless, not a superuser, owns the database and
#   has no memberships;
# - the groups are NOLOGIN capability holders that own nothing;
# - the groups' members, transitively, are exactly the generation's logins, and
#   each is exactly shaped (`roles.verify_generation`);
# - every other login (auxiliary: replication, operator read-only) is not a
#   superuser, owns nothing and holds no write privilege on `public`;
# - the stable monitoring login, when present, is exactly its shape
#   (`monitor.monitor_violations`).

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from base.cluster.authority.catalog import (
    BOOTSTRAP_SUPERUSER_OID,
    Conn,
    group_members,
    login_roles,
    memberships,
    require_admin,
    role_facts,
    transitive_members,
)
from base.cluster.authority.checks import (
    check_membership_facts,
    check_owner_facts,
)
from base.cluster.authority.groups import group_violations
from base.cluster.authority.monitor import monitor_violations
from base.cluster.authority.roles import verify_generation
from base.host.db_authority.ledger import require_ledger
from base.host.db_authority.model import (
    AuthorityRefusedError,
    CatalogRefusedError,
    Ledger,
    VerifiedGeneration,
)


def _owner_violations(conn: Conn, ledger: Ledger, database: str) -> list[str]:
    fact = role_facts(conn, (ledger.owner,)).get(ledger.owner)
    if fact is None:
        return [f"schema owner {ledger.owner} does not exist"]
    owner_memberships = memberships(conn, (ledger.owner,))
    row = conn.execute(
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s", (database,)
    ).fetchone()
    return check_owner_facts(
        ledger.owner,
        fact,
        owner_memberships,
        database=database,
        database_owner=None if row is None else row[0],
    )


def _membership_violations(conn: Conn, ledger: Ledger) -> list[str]:
    expected: set[str] = set(ledger.generation.roles) if ledger.generation is not None else set()
    return check_membership_facts(
        expected_roles=expected,
        actual_group_members=group_members(conn, ledger.groups),
        owner_members=transitive_members(conn, (ledger.owner,)),
    )


_WRITES = """
SELECT c.oid::regclass::text, p.privilege
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE']) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND has_table_privilege(%(role)s, c.oid, p.privilege)
UNION ALL
SELECT c.oid::regclass::text, p.privilege
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN unnest(ARRAY['USAGE', 'UPDATE']) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind = 'S'
  AND has_sequence_privilege(%(role)s, c.oid, p.privilege)
ORDER BY 1, 2
LIMIT 5
"""


def _auxiliary_violations(conn: Conn, ledger: Ledger) -> list[str]:
    active: set[str] = set(ledger.generation.roles) if ledger.generation is not None else set()
    violations: list[str] = []
    for name, fact in sorted(login_roles(conn).items()):
        if fact.oid == BOOTSTRAP_SUPERUSER_OID or name in active:
            continue
        if fact.superuser:
            violations.append(f"login {name} is an unexpected superuser")
            continue
        if fact.owns:
            violations.append(f"login {name} owns objects")
        writes = conn.execute(_WRITES, {"role": name}).fetchall()
        if writes:
            violations.append(f"login {name} holds write privileges {writes}")
    return violations


def check_invariant(
    conn: Conn, home: Path, *, database: str, readonly_grantees: Iterable[str] = ()
) -> VerifiedGeneration | None:
    """Prove the catalog equals the ledger; return the generation's receipt.

    Read-only. ``conn`` must be the admin session connected to ``database``.
    ``readonly_grantees`` names operator roles allowed SELECT/USAGE/CONNECT.
    """
    del readonly_grantees
    require_admin(conn)
    row = conn.execute("SELECT current_database()").fetchone()
    if row is None or row[0] != database:
        raise AuthorityRefusedError("the invariant must run connected to the cluster database")
    ledger = require_ledger(home)
    violations = _owner_violations(conn, ledger, database)
    violations += group_violations(conn, ledger.groups)
    violations += [
        f"capability group {name} does not exist"
        for name in (ledger.groups.gateway, ledger.groups.runner)
        if name not in role_facts(conn, (name,))
    ]
    violations += _membership_violations(conn, ledger)
    violations += _auxiliary_violations(conn, ledger)
    violations += monitor_violations(conn, database=database)
    verified: VerifiedGeneration | None = None
    if ledger.generation is not None:
        try:
            verified = verify_generation(conn, home)
        except CatalogRefusedError as refusal:
            violations += refusal.violations
    if violations:
        raise CatalogRefusedError(tuple(violations))
    return verified
