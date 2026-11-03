#!/usr/bin/env python3
# meiva:note
# Assign (or clear) one schedule's owner -- the agent a schedule speaks as -- and record why.
#
# This is the only path that changes an owner. The HTTP API refuses `owner_agent_id` on create and
# update because the gateway cannot tell its callers apart: one shared administrator credential,
# caller labels deliberately absent. Without a caller identity, an API-exposed owner field lets any
# caller make an unattended script write under another agent's name. Here the operator is on the
# gateway host, holding the home's database, and the reason they type is the only evidence the
# assignment will ever have -- so it is required, and it is stored.
#
# The run is one transaction: the schedules row and its `schedule_owner_changes` row commit
# together, or neither does.
# meiva:link scripts/host_ops/docs/set-schedule-owner.meiva.okf.md
from __future__ import annotations

import argparse
import socket
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import class_row


def _positive_id(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("expected a positive id")
    return number


# meiva:note
# The reason is the audit. Whitespace is not a reason, and neither is a stub the next operator
# cannot act on, so an empty or blank value stops the run before it touches a row.
def reason_argument(value: str) -> str:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("--reason must say why this owner is right")
    return text


# meiva:note
# The parsed flags as a typed namespace: argparse fills the declared attributes with the values its
# own `type=` and `action=` converted, so the run reads `int`/`bool`/`str` rather than `Any`.
class _Arguments(argparse.Namespace):
    schedule_id: int
    owner: int | None
    clear: bool
    reason: str
    dry_run: bool


# meiva:note
# One schedule's current owner, read as a typed row: the audit's "before" side.
@dataclass(frozen=True)
class _OwnerRow:
    owner_agent_id: int | None


def _parse(argv: list[str]) -> _Arguments:
    parser = argparse.ArgumentParser(description="Assign or clear a schedule's owner_agent_id.")
    parser.add_argument("schedule_id", type=_positive_id)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner", type=_positive_id, help="agent id this schedule speaks as")
    target.add_argument(
        "--clear", action="store_true", help="stop speaking as an agent (owner_agent_id = NULL)"
    )
    parser.add_argument("--reason", required=True, type=reason_argument, help="why this assignment")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the change and roll back instead of committing",
    )
    return parser.parse_args(argv, namespace=_Arguments())


# meiva:note
# Whether the owner names a real agent. The check runs inside the assignment's own transaction, so a
# row deleted between the two statements rolls the assignment back with it.
def _agent_exists(conn: psycopg.Connection, agent_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents WHERE id = %s", (agent_id,))
        return cur.fetchone() is not None


def assign_owner(
    conn: psycopg.Connection, schedule_id: int, owner: int | None, reason: str
) -> tuple[int | None, str]:
    with conn.cursor(row_factory=class_row(_OwnerRow)) as cur:
        cur.execute("SELECT owner_agent_id FROM schedules WHERE id = %s FOR UPDATE", (schedule_id,))
        row = cur.fetchone()
        if row is None:
            raise SystemExit(f"schedule {schedule_id}: no such schedule")
        previous = row.owner_agent_id
        if previous == owner:
            return previous, "unchanged"
        if owner is not None and not _agent_exists(conn, owner):
            raise SystemExit(f"agent {owner}: no such agent")
        cur.execute("UPDATE schedules SET owner_agent_id = %s WHERE id = %s", (owner, schedule_id))
        cur.execute(
            "INSERT INTO schedule_owner_changes "
            "(schedule_id, old_owner_id, new_owner_id, source, reason) "
            "VALUES (%s, %s, %s, %s, %s)",
            (schedule_id, previous, owner, f"ops:{socket.gethostname()}", reason),
        )
    return previous, "updated"


def main(argv: list[str]) -> int:
    args = _parse(argv)
    from base.config import settings

    owner: int | None = None if args.clear else args.owner
    with psycopg.connect(settings.data_plane.db_url) as conn:
        previous, outcome = assign_owner(conn, args.schedule_id, owner, args.reason)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    suffix = ", dry run" if args.dry_run else ""
    print(f"schedule {args.schedule_id}: owner {previous} -> {owner} ({outcome}, {stamp}{suffix})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
