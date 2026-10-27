# meiva:note
# What one round of stale-shell sweeping asks and how it reads the answer, decided from values.
#
# `plan_stale` groups the unexpired tracking rows whose session may be gone by machine and then by
# agent (the `shell_probe` op answers for one agent), `probe_session_ids` reads that answer into the
# session ids the host lists or says it proves nothing, and `stale_rows` keeps the rows the host did
# not list. `absence_is_terminal` decides whether a machine's absence from the registry may be read
# as "the machine is gone", from the last probe anything recorded of it. The shell dials the
# machine, reads the probe record and deletes the rows; nothing here reads a clock, a registry or a
# connection.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeGuard

from services.upkeep.ttl_reaper.shell_plan import UNKNOWN_MACHINE, ShellRow


# meiva:note
# The unexpired rows whose session may be gone, grouped by machine and then by agent: the
# `shell_probe` op answers for one agent on one host, so an agent is the finest grain a round can
# ask about, and one listing covers every row of that agent. The machine level is what a round
# schedules on — one failed probe means the host is not answering, and the machine's remaining
# agents wait for the next round instead of repeating the failure. Machines and agents keep
# first-seen order.
@dataclass(frozen=True)
class AgentRows:
    agent_id: int
    rows: tuple[ShellRow, ...]


@dataclass(frozen=True)
class MachineProbes:
    machine: str
    agents: tuple[AgentRows, ...]


@dataclass(frozen=True)
class StalePlan:
    machines: tuple[MachineProbes, ...]


# meiva:note
# The reader's select already excludes rows whose owner has no usable machine, so the guard below is
# a guard and not a path: such a row has nothing to dial and is left to the deadline path.
def plan_stale(rows: Sequence[ShellRow]) -> StalePlan:
    grouped: dict[str, dict[int, list[ShellRow]]] = {}
    for row in rows:
        if not row.machine or row.machine == UNKNOWN_MACHINE:
            continue
        grouped.setdefault(row.machine, {}).setdefault(row.agent_id, []).append(row)
    return StalePlan(
        machines=tuple(
            MachineProbes(
                machine,
                tuple(AgentRows(agent_id, tuple(group)) for agent_id, group in agents.items()),
            )
            for machine, agents in grouped.items()
        )
    )


# meiva:note
# A JSON list. The second reader narrows the entries of one: a runner's answer crosses a network
# boundary, so a listing is only a listing once every entry is an object — anything else makes the
# whole answer unusable.
def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_rows(value: object) -> TypeGuard[list[Mapping[str, object]]]:
    return _is_object_list(value) and all(isinstance(entry, Mapping) for entry in value)


# meiva:note
# The session ids a runner's `shell_probe` answer lists, or None when the answer carries no usable
# listing (no `shells`, something else than a list, or an entry without an integer `id`). None
# means the answer proves nothing and the caller keeps every row; an empty set is a real answer —
# the host says this agent has no live shell on it. Ids are agent-local, so a set is comparable
# only with rows of the same agent on the same machine, which is how `plan_stale` groups them.
def probe_session_ids(answer: Mapping[str, object]) -> frozenset[int] | None:
    shells = answer.get("shells")
    if not _is_object_rows(shells):
        return None
    ids: set[int] = set()
    for entry in shells:
        value = entry.get("id")
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        ids.add(value)
    return frozenset(ids)


# meiva:note
# The rows whose session the host did not list. A session that is not there cannot be renewed or
# killed, so its row is only a leak: it inflates the "unexpired tracking rows" count a duty's
# health check reads and would keep the row until its declared deadline, up to a year away.
def stale_rows(rows: Sequence[ShellRow], live: frozenset[int]) -> tuple[ShellRow, ...]:
    return tuple(row for row in rows if row.session_id not in live)


# meiva:note
# Whether a machine's absence from the machines registry may be read as "that machine is gone".
#
# The registry is a table, not the world: a rebuild, a restore, a rename, a rollout that writes it,
# or one bad bulk update all make a live machine disappear from it for a while. Deleting a live
# session's tracking row is irreversible — nothing can renew or reclaim that session again, and the
# "unexpired rows" count a duty's health check reads drops with it — so absence alone is not
# evidence. It becomes evidence only together with silence: nothing anywhere has recorded a probe of
# that machine inside the same window a young row is exempt for. A machine that answers no probe for
# six hours and is missing from the registry is gone, and its rows can never be dialed again.
def absence_is_terminal(last_probe_at: datetime | None, *, now: datetime, grace: timedelta) -> bool:
    return last_probe_at is None or last_probe_at < now - grace
