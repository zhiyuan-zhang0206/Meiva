# meiva:note
# The schedule rows the API reads, as the database returns them: one frozen dataclass per query
# shape, a field per selected column named as the column. `ScheduleFullRow` is also what a keyed
# operation's receipt stores (in this field order, datetimes as ISO strings), so a replay
# validates back into the same class.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

__all__ = [
    "ControlRow",
    "DeletedRow",
    "InsertedId",
    "LaunchEnvRow",
    "OwnerRow",
    "PlacementRow",
    "ScheduleFullRow",
    "ScheduleStoreRow",
    "ScheduleSummaryRow",
]


# meiva:note
# The machine facts a placement verdict reads (`mp.online` is NULL for a machine never probed).
@dataclass(frozen=True)
class PlacementRow:
    machine_registered: bool
    paused_at: datetime | None
    pause_reason: str | None
    stopped_at: datetime | None
    maintenance_reason: str | None
    online: bool | None
    consecutive_failures: int | None


# meiva:note
# A schedule without its script, with the placement facts of its machine (the list query).
@dataclass(frozen=True)
class ScheduleSummaryRow:
    id: int
    name: str
    description: str | None
    command: str
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    machine: str
    owner_plugin: str | None
    machine_registered: bool
    paused_at: datetime | None
    pause_reason: str | None
    stopped_at: datetime | None
    maintenance_reason: str | None
    online: bool | None
    consecutive_failures: int | None
    owner_agent_id: int | None


# meiva:note
# A schedule with its script and the placement facts of its machine (the one-schedule query).
# A NamedTuple, not a dataclass: `fetch_full_blocking` / `update_blocking` have always returned the
# row as a tuple in this order, and the operator repair script (`scripts/host_ops/
# update_schedule_script.py`) runs against whichever gateway source is installed, so it still
# indexes and slices the row. The fields are the typed names of those positions.
class ScheduleFullRow(NamedTuple):
    id: int
    name: str
    description: str | None
    command: str
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    machine: str
    owner_plugin: str | None
    script: str
    machine_registered: bool
    paused_at: datetime | None
    pause_reason: str | None
    stopped_at: datetime | None
    maintenance_reason: str | None
    online: bool | None
    consecutive_failures: int | None
    owner_agent_id: int | None


# meiva:note
# A schedule as the `schedules` table stores it (read under `FOR UPDATE` before an edit).
@dataclass(frozen=True)
class ScheduleStoreRow:
    id: int
    name: str
    description: str | None
    command: str
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    machine: str
    owner_plugin: str | None
    script: str
    owner_agent_id: int | None


# meiva:note
# The id a schedule INSERT returns.
@dataclass(frozen=True)
class InsertedId:
    id: int


# meiva:note
# Who owns a schedule: the plugin that declared it, NULL for operator-created and builtin ones.
@dataclass(frozen=True)
class OwnerRow:
    owner_plugin: str | None


# meiva:note
# The machine a deleted schedule was assigned to.
@dataclass(frozen=True)
class DeletedRow:
    machine: str


# meiva:note
# What a start / stop / restart reads under `FOR UPDATE`.
@dataclass(frozen=True)
class ControlRow:
    enabled: bool
    status: str
    machine: str


# meiva:note
# Whether a schedule is enabled and the secret environment names it declares.
@dataclass(frozen=True)
class LaunchEnvRow:
    enabled: bool
    secret_env_vars: list[str]
