# meiva:note
# `meiva cluster ops` — the readable entry for cluster lifecycle operations (task #6024).
#
# Every `meiva stop`/`start`/`restart` already writes `lifecycle_request` and `lifecycle_result`
# rows into `audit_events`, carrying the caller block, the target commit and — since the fleet
# window mints them — one `operation_id` per window, the `initiator`, and the release gate's
# verdict. Those rows were reachable only by hand-written SQL, so a five-host restart read as
# "nobody can say who did it". This command is the reading surface: one line per operation,
# `--operation` to expand one window to its per-host rows.
# meiva:link base/native_process/root_control/docs/lifecycle-caller.meiva.okf.md

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, TypeGuard

from base.agents.context.clients import DatabaseFactory

_LIFECYCLE_EVENTS = ("lifecycle_request", "lifecycle_result")
_DEFAULT_HOURS = 24
_DEFAULT_LIMIT = 200
_NO_VALUE = "-"
# meiva:note
# A lifecycle_result row is written on the orchestrating side and carries no caller block, so the
# machine it ran on is unknown. Counting that bucket as a host once reported a six-host window for
# a five-machine fleet (2026-11-03); the window is per-operation, not per-host, so an unattributed
# row must not add one.
_UNATTRIBUTED_MACHINE = "?"


def _is_obj(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


@dataclass(frozen=True)
class OperationRow:
    ts: datetime
    event_name: str
    attributes: Mapping[str, Any]

    @property
    def operation_id(self) -> str | None:
        value = self.attributes.get("operation_id")
        return value if isinstance(value, str) and value else None

    @property
    def verb(self) -> str:
        value = self.attributes.get("verb")
        return value if isinstance(value, str) else "?"

    @property
    def machine(self) -> str:
        caller = self.attributes.get("caller")
        value = caller.get("machine") if _is_obj(caller) else None
        return value if isinstance(value, str) else _UNATTRIBUTED_MACHINE

    def _text(self, key: str) -> str | None:
        value = self.attributes.get(key)
        return value if isinstance(value, str) and value else None

    # meiva:note
    # Who started the window, as the window stamped it. Falls back to the caller block a
    # pre-#6024 command left: the machine that ran the command and the ssh source are still an
    # attribution, just a weaker one.
    @property
    def initiator(self) -> str | None:
        stamped = self._text("initiator")
        if stamped is not None:
            return stamped
        caller = self.attributes.get("caller")
        spawn = self.attributes.get("spawn")
        machine = caller.get("machine") if _is_obj(caller) else None
        source = spawn.get("ssh_connection") if _is_obj(spawn) else None
        if isinstance(machine, str) and isinstance(source, str):
            return f"{machine} <- {source.split(' ')[0]}"
        return machine if isinstance(machine, str) else None

    @property
    def gate(self) -> str | None:
        return self._text("gate")

    @property
    def gate_reason(self) -> str | None:
        return self._text("gate_reason")


@dataclass(frozen=True)
class Operation:
    key: str
    rows: tuple[OperationRow, ...]

    @property
    def started(self) -> datetime:
        return min(row.ts for row in self.rows)

    @property
    def hosts(self) -> list[str]:
        return sorted({row.machine for row in self.rows if row.machine != _UNATTRIBUTED_MACHINE})

    @property
    def verbs(self) -> list[str]:
        return sorted({row.verb for row in self.rows})

    @property
    def outcome(self) -> str:
        results = [
            str(row.attributes.get("outcome"))
            for row in self.rows
            if row.event_name == "lifecycle_result" and row.attributes.get("outcome")
        ]
        if not results:
            return _NO_VALUE
        failed = sum(1 for value in results if value != "ok")
        return "ok" if not failed else f"{failed} failed"


# meiva:note
# Group rows by the operation id a window stamped; ungrouped rows stay one operation each. A row
# with no `operation_id` is a command an operator typed (or a window that predates the stamp):
# grouping those together would invent one operation out of unrelated stops, so each keeps its
# own key.
def _operations(rows: list[OperationRow]) -> list[Operation]:
    groups: dict[str, list[OperationRow]] = defaultdict(list)
    for row in rows:
        key = row.operation_id or f"single:{row.ts.isoformat()}:{row.machine}"
        groups[key].append(row)
    return [
        Operation(key=key, rows=tuple(sorted(items, key=lambda r: r.ts)))
        for key, items in groups.items()
    ]


_SQL = """
select ts, event_name, attributes::text
from audit_events
where event_name = any(%s)
  and ts >= now() - make_interval(hours => %s)
order by ts desc
limit %s
"""


# meiva:note
# Read the lifecycle rows the audit already holds, over the operator's own connection (the same
# `autocommit` read the health probe uses). `attributes` is rendered to text and parsed here so
# the reader does not depend on the connection's row factory — the gateway reads the same rows
# with a different one.
def _read(database_factory: DatabaseFactory, *, hours: int, limit: int) -> list[OperationRow]:
    with database_factory().connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_SQL, (list(_LIFECYCLE_EVENTS), hours, limit))
        fetched: list[tuple[object, ...]] = cur.fetchall()
    rows: list[OperationRow] = []
    for row in fetched:
        ts, event_name, raw = row[0], row[1], row[2]
        parsed: object = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(ts, datetime) or not isinstance(event_name, str):
            continue
        rows.append(
            OperationRow(
                ts=ts,
                event_name=event_name,
                attributes=MappingProxyType(parsed) if _is_obj(parsed) else MappingProxyType({}),
            )
        )
    return rows


def cmd_cluster_ops(
    *,
    database_factory: DatabaseFactory,
    hours: int = _DEFAULT_HOURS,
    limit: int = _DEFAULT_LIMIT,
    operation: str | None = None,
) -> int:
    print(f"reading the last {hours}h of lifecycle operations (limit {limit} rows)")
    rows = _read(database_factory, hours=hours, limit=limit)
    if not rows:
        print("no lifecycle operations in that window")
        return 0
    operations = _operations(rows)
    if operation is not None:
        selected = [op for op in operations if op.key == operation]
        if not selected:
            print(f"no operation {operation!r} in that window")
            return 1
        operations = selected
    for op in sorted(operations, key=lambda o: o.started, reverse=True):
        gate = op.rows[0].gate or _NO_VALUE
        reason = op.rows[0].gate_reason
        gate_text = f"{gate} ({reason})" if reason else gate
        print(
            f"{op.started:%Y-%m-%d %H:%M:%S}  {op.key[:24]:24s}  "
            f"{'/'.join(op.verbs):10s}  {len(op.hosts)} host(s)  {op.outcome:10s}  "
            f"gate={gate_text}  initiator={op.rows[0].initiator or _NO_VALUE}"
        )
        if operation is not None:
            for row in op.rows:
                print(f"    {row.ts:%H:%M:%S}  {row.machine:22s}  {row.verb:8s}  {row.event_name}")
    return 0
