"""Read-only PostgreSQL catalog facts the authority library decides on.

Every function takes the caller's admin connection: the OS-user superuser
session over the owner-only socket, validated for custody by its opener. This
module never opens a connection and never infers authority from a role name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import psycopg

from base.cluster.authority.checks import (
    BOOTSTRAP_SUPERUSER_OID,
    Membership,
    RoleFacts,
)
from base.db.rows import query_all
from base.host.db_authority.model import AuthorityRefusedError, Groups

__all__ = [
    "BOOTSTRAP_SUPERUSER_OID",
    "Conn",
    "Membership",
    "RoleFacts",
    "group_members",
    "login_roles",
    "memberships",
    "require_admin",
    "role_facts",
    "transitive_members",
]

Conn = psycopg.Connection


# meiva:note
# The answer of the statements that select one `rolname` column.
@dataclass(frozen=True)
class _RolnameRow:
    rolname: str


def require_admin(conn: Conn) -> None:
    """Refuse anything but an autocommit superuser session without SET ROLE.

    Autocommit makes each authority step its own committed transaction, so a
    durable boundary is exactly where the code says it is.
    """
    if not conn.autocommit:
        raise AuthorityRefusedError("database authority requires an autocommit admin session")
    row = conn.execute(
        "SELECT r.rolsuper, session_user = current_user"
        " FROM pg_roles r WHERE r.rolname = session_user"
    ).fetchone()
    if row != (True, True):
        raise AuthorityRefusedError(
            "database authority requires the superuser session without SET ROLE"
        )


_ROLE_FACTS = """
SELECT a.oid AS oid, a.rolname AS name, a.rolcanlogin AS login, a.rolsuper AS superuser,
       a.rolinherit AS inherit, a.rolcreaterole AS createrole, a.rolcreatedb AS createdb,
       a.rolreplication AS replication, a.rolbypassrls AS bypassrls,
       a.rolconnlimit AS connection_limit, a.rolvaliduntil AS valid_until,
       a.rolpassword AS password,
       EXISTS (SELECT 1 FROM pg_db_role_setting s WHERE s.setrole = a.oid) AS configured,
       EXISTS (SELECT 1 FROM pg_shdepend d
               WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = a.oid) AS depended,
       EXISTS (SELECT 1 FROM pg_shdepend d
               WHERE d.refclassid = 'pg_authid'::regclass AND d.refobjid = a.oid
                 AND d.deptype = 'o') AS owns
FROM pg_authid a
WHERE a.rolname = ANY(%s)
"""


def role_facts(conn: Conn, names: Iterable[str]) -> dict[str, RoleFacts]:
    """Catalog facts for the named roles that exist (absent names are omitted)."""
    rows = query_all(conn, RoleFacts, _ROLE_FACTS, (sorted(set(names)),))
    return {row.name: row for row in rows}


def login_roles(conn: Conn) -> dict[str, RoleFacts]:
    """Facts for every role that can currently log in."""
    rows = query_all(conn, _RolnameRow, "SELECT rolname FROM pg_roles WHERE rolcanlogin")
    names = [row.rolname for row in rows]
    return role_facts(conn, names)


_MEMBERSHIPS = """
SELECT g.rolname AS role, m.rolname AS member, gr.rolname AS grantor,
       am.admin_option AS admin, am.inherit_option AS inherit, am.set_option AS "set"
FROM pg_auth_members am
JOIN pg_roles g ON g.oid = am.roleid
JOIN pg_roles m ON m.oid = am.member
JOIN pg_roles gr ON gr.oid = am.grantor
WHERE g.rolname = ANY(%(names)s) OR m.rolname = ANY(%(names)s)
ORDER BY 1, 2, 3
"""


def memberships(conn: Conn, names: Iterable[str]) -> tuple[Membership, ...]:
    """Every membership row in which a named role is the role or the member."""
    return tuple(query_all(conn, Membership, _MEMBERSHIPS, {"names": sorted(set(names))}))


_TRANSITIVE_MEMBERS = """
WITH RECURSIVE members(oid) AS (
    SELECT am.member FROM pg_auth_members am
    JOIN pg_roles g ON g.oid = am.roleid
    WHERE g.rolname = ANY(%s)
    UNION
    SELECT am.member FROM pg_auth_members am JOIN members ON am.roleid = members.oid
)
SELECT r.rolname FROM members JOIN pg_roles r ON r.oid = members.oid ORDER BY 1
"""


def transitive_members(conn: Conn, roles: Iterable[str]) -> tuple[str, ...]:
    """Every role holding membership in ``roles``, directly or through another role."""
    rows = query_all(conn, _RolnameRow, _TRANSITIVE_MEMBERS, (sorted(set(roles)),))
    return tuple(row.rolname for row in rows)


def group_members(conn: Conn, groups: Groups) -> tuple[str, ...]:
    return transitive_members(conn, (groups.gateway, groups.runner))
