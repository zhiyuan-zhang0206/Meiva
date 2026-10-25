from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

BOOTSTRAP_SUPERUSER_OID = 10
MONITOR_ROLE = "meiva_monitor"
MONITOR_MAP = "meiva_monitor"
STATS_ROLE = "pg_read_all_stats"

PUBLIC_ALLOWED = frozenset({"EXECUTE", "USAGE", "TEMPORARY"})
READONLY_ALLOWED = frozenset({"SELECT", "USAGE", "CONNECT"})


@dataclass(frozen=True)
class RoleFacts:
    oid: int
    name: str
    login: bool
    superuser: bool
    inherit: bool
    createrole: bool
    createdb: bool
    replication: bool
    bypassrls: bool
    connection_limit: int
    valid_until: datetime | None
    # meiva:note
    # `pg_authid.rolpassword` is a SCRAM verifier, not the plaintext login, but
    # still credential material; hidden from repr for the same reason as
    # `RoleSecret.verifier` (base/host/db_authority/model.py).
    password: str | None = field(repr=False)
    configured: bool
    depended: bool
    owns: bool

    @property
    def elevated(self) -> tuple[str, ...]:
        flags = {
            "SUPERUSER": self.superuser,
            "CREATEROLE": self.createrole,
            "CREATEDB": self.createdb,
            "REPLICATION": self.replication,
            "BYPASSRLS": self.bypassrls,
        }
        return tuple(name for name, held in flags.items() if held)


@dataclass(frozen=True)
class Membership:
    role: str
    member: str
    grantor: str
    admin: bool
    inherit: bool
    set: bool


def check_group_facts(
    group_names: tuple[str, str],
    facts: Mapping[str, RoleFacts],
    memberships: Sequence[Membership],
) -> list[str]:
    violations: list[str] = []
    for name, fact in facts.items():
        if fact.login or fact.password is not None:
            violations.append(f"group {name} can log in")
        if fact.elevated or fact.owns or fact.configured:
            violations.append(f"group {name} holds attributes, ownership or settings")
        if not fact.inherit:
            violations.append(f"group {name} is NOINHERIT")
    for row in memberships:
        if row.member in group_names:
            violations.append(f"group {row.member} is a member of {row.role}")
    return violations


def _check_monitor_attributes(fact: RoleFacts) -> list[str]:
    name = fact.name
    violations: list[str] = []
    if not fact.login:
        violations.append(f"{name} cannot log in")
    if fact.password is not None:
        violations.append(f"{name} has a password; it authenticates only by peer")
    if fact.elevated or not fact.inherit or fact.configured:
        violations.append(f"{name} holds attributes, NOINHERIT or settings")
    if fact.connection_limit != -1 or fact.valid_until is not None:
        violations.append(f"{name} carries a connection limit or expiry")
    return violations


def check_monitor_facts(
    fact: RoleFacts,
    *,
    database: str,
    database_oid: object,
    memberships: Sequence[Membership],
    grants: Sequence[tuple[object, ...]],
) -> list[str]:
    name = fact.name
    violations = _check_monitor_attributes(fact)
    held = [
        (row.role, row.admin, row.inherit, row.set) for row in memberships if row.member == name
    ]
    if held != [(STATS_ROLE, False, True, False)]:
        violations.append(f"{name} memberships {held} differ from INHERIT-only {STATS_ROLE}")
    if any(row.role == name for row in memberships):
        violations.append(f"{name} has members")
    expected = [(0, "pg_database", database_oid, "a")]
    if list(grants) != expected:
        violations.append(f"{name} owns objects or holds grants beyond CONNECT on {database}")
    return violations


def _check_role_shape(fact: RoleFacts, verifier: str) -> list[str]:
    name = fact.name
    violations: list[str] = []
    if not fact.login:
        violations.append(f"{name} cannot log in")
    if fact.elevated:
        violations.append(f"{name} holds {', '.join(fact.elevated)}")
    if not fact.inherit:
        violations.append(f"{name} is NOINHERIT")
    if fact.connection_limit != -1 or fact.valid_until is not None:
        violations.append(f"{name} carries a connection limit or expiry")
    if fact.password != verifier:
        violations.append(f"{name} stored verifier differs from its secret")
    if fact.configured:
        violations.append(f"{name} carries role settings")
    if fact.depended:
        violations.append(f"{name} owns objects or holds direct grants")
    return violations


def check_login_role_facts(
    role_name: str,
    verifier: str,
    group: str,
    fact: RoleFacts | None,
    memberships: Sequence[Membership],
) -> list[str]:
    if fact is None:
        return [f"{role_name} does not exist"]
    violations = _check_role_shape(fact, verifier)
    held = [
        (row.role, row.admin, row.inherit, row.set)
        for row in memberships
        if row.member == role_name
    ]
    if held != [(group, False, True, False)]:
        violations.append(f"{role_name} memberships {held} differ from INHERIT-only {group}")
    if any(row.role == role_name for row in memberships):
        violations.append(f"{role_name} has members")
    return violations


def check_owner_facts(
    owner: str,
    fact: RoleFacts | None,
    memberships: Sequence[Membership],
    *,
    database: str,
    database_owner: str | None,
) -> list[str]:
    if fact is None:
        return [f"schema owner {owner} does not exist"]
    violations: list[str] = []
    if fact.login or fact.password is not None:
        violations.append(f"schema owner {owner} can log in")
    if fact.oid == BOOTSTRAP_SUPERUSER_OID or fact.elevated or fact.configured:
        violations.append(f"schema owner {owner} holds attributes or settings")
    if memberships:
        violations.append(f"schema owner {owner} has memberships")
    if database_owner != owner:
        violations.append(f"database {database} is not owned by {owner}")
    return violations


def check_membership_facts(
    *,
    expected_roles: Iterable[str],
    actual_group_members: Iterable[str],
    owner_members: Sequence[str],
) -> list[str]:
    extra = sorted(set(actual_group_members) - set(expected_roles))
    violations = [f"unexpected group members {extra}"] if extra else []
    if owner_members:
        violations.append(f"roles hold schema-owner membership: {list(owner_members)}")
    return violations


def _grant_allowed(
    *,
    owner: str,
    groups: tuple[str, str],
    grantee: str,
    privilege: str,
    own: bool,
    readonly: frozenset[str],
) -> bool:
    if own or grantee in {owner, *groups}:
        return True
    if grantee == "PUBLIC":
        return privilege in PUBLIC_ALLOWED
    if grantee == MONITOR_ROLE:
        return privilege == "CONNECT"
    return grantee in readonly and privilege in READONLY_ALLOWED


def check_acl_facts(
    *,
    owner: str,
    groups: tuple[str, str],
    readonly: frozenset[str],
    acls: Iterable[tuple[str, str, str, bool]],
    default_acls: Iterable[tuple[str, str, str, bool]],
    public_can_connect: bool,
) -> list[str]:
    violations = [
        f"{target} grants {privilege} to {grantee}"
        for target, grantee, privilege, own in acls
        if not _grant_allowed(
            owner=owner,
            groups=groups,
            grantee=grantee,
            privilege=privilege,
            own=own,
            readonly=readonly,
        )
    ]
    for role, grantee, privilege, own in default_acls:
        if role != owner:
            violations.append(f"default privileges declared for {role}")
        elif not _grant_allowed(
            owner=owner,
            groups=groups,
            grantee=grantee,
            privilege=privilege,
            own=own,
            readonly=readonly,
        ):
            violations.append(f"default privileges grant {privilege} to {grantee}")
    if public_can_connect:
        violations.append("PUBLIC may connect to the cluster database")
    return violations
