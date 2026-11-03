# meiva:note
# The schedule API's request and response shapes, kept apart from the router that serves them:
# the router is I/O orchestration and already sits at its file budget, and these are the wire
# contract a client (the CLI, the UI) and the router must agree on.
#
# `ScheduleSummary` names `placement_status`, whose values come from `placement.py`; the
# dependency runs one way (contracts -> placement), so the router can import both without a
# cycle.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from gateway.schedules.placement import PlacementStatus

__all__ = [
    "DEFAULT_COMMAND",
    "ScheduleCreate",
    "ScheduleDraftRequest",
    "ScheduleDraftResponse",
    "ScheduleLogsView",
    "ScheduleRunView",
    "ScheduleSummary",
    "ScheduleUpdate",
    "ScheduleView",
]

DEFAULT_COMMAND = "python schedule.py"


class ScheduleSummary(BaseModel):
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
    owner_plugin: str | None = None
    owner_agent_id: int | None = None
    placement_status: PlacementStatus
    placement_reason: str | None = None


class ScheduleView(ScheduleSummary):
    script: str


class ScheduleCreate(BaseModel):
    name: str
    script: str
    command: str = DEFAULT_COMMAND
    description: str | None = None
    enabled: bool = True
    machine: str | None = None
    owner_agent_id: int | None = None


class ScheduleUpdate(BaseModel):
    name: str | None = None
    script: str | None = None
    command: str | None = None
    description: str | None = None
    enabled: bool | None = None
    machine: str | None = None
    owner_agent_id: int | None = None


class ScheduleRunView(BaseModel):
    id: int
    ran_at: datetime
    ok: bool | None
    agent_id: int | None
    note: str | None


class ScheduleLogsView(BaseModel):
    source: str
    lines: list[str]


class ScheduleDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language description of the scheduled task.")


class ScheduleDraftResponse(BaseModel):
    agent_id: int
