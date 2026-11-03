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
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import class_row

from base.host.env.boot_policy import PLACEHOLDER_DB_URL
from base.paths.home import resolve_meiva_home


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


# meiva:note
# The reason the run cannot dial, or None when it can. The check sits at the connection, not at
# settings load, because a process that deliberately carries the placeholder is a legitimate way to
# say "this box has no database" -- refusing there would turn every such tool red.
#
# The comparison is whole-string, the same one `base/cluster/dataplane/direct_url.py:30` makes -- this
# is the repository's standing reading of the placeholder, not a comparison invented here. It holds
# only while settings hands back the file's value unchanged; if `base/config/service_read.py` ever
# applies its `project_service_db_url` projection to this URL, compare components instead.
#
# The message names the two halves of the trap this tool hit once: the address it read and where it
# came from, and the variable it ignored. A bare "connection refused" sent its first reader looking
# for a network fault. It forks on a fact, because an absent .env file and a .env without the key
# send the reader to different places -- the first to the home it named, the second to the file. The
# URL is never printed whole: it carries the password.
def unresolvable_reason(db_url: str, home: Path) -> str | None:
    if db_url != PLACEHOLDER_DB_URL:
        return None
    parts = urlsplit(db_url)
    env_file = home / ".env"
    address = f"{parts.hostname}:{parts.port}"
    ignored = (
        "The process environment's MEIVA_DB_URL is not read by settings, so exporting it in the "
        "shell changes nothing."
    )
    if not env_file.is_file():
        return (
            f"no database to assign an owner in: {env_file} does not exist, so settings fell back "
            f"to the boot placeholder ({address}). {ignored} If the home MEIVA_HOME names is not "
            f"the one you meant, point it at the one that holds a .env."
        )
    return (
        f"no database to assign an owner in: settings resolved {address} from {env_file}, which "
        f"carries no MEIVA_DB_URL or carries the placeholder itself. {ignored} Put it in "
        f"{env_file}, or point MEIVA_HOME at the home that holds one."
    )


# meiva:note
# The connection this run writes through: refuse a placeholder home before dialing, then hand the
# URL to psycopg. Both facts come in as data so a test can drive every direction without patching.
def connect(db_url: str, home: Path) -> psycopg.Connection:
    refusal = unresolvable_reason(db_url, home)
    if refusal is not None:
        raise SystemExit(refusal)
    return psycopg.connect(db_url)


def main(argv: list[str]) -> int:
    args = _parse(argv)
    from base.config import settings

    owner: int | None = None if args.clear else args.owner
    with connect(settings.data_plane.db_url, resolve_meiva_home()) as conn:
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
