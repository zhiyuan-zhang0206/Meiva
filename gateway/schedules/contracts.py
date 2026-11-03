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

from pydantic import BaseModel, Field, field_validator

from gateway.schedules.placement import PlacementStatus

__all__ = [
    "DEFAULT_COMMAND",
    "OWNER_VIA_PROVISIONING",
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

# meiva:note
# `owner_agent_id` is a provisioning fact, not an API field. Whoever a schedule speaks as decides
# whose name its unattended writes carry, and the gateway cannot see the caller (one shared
# administrator credential, caller labels deliberately absent), so no API caller can be allowed to
# set it for another agent. The field stays declared only so a caller that sends it gets this
# sentence back instead of a silently ignored key.
OWNER_VIA_PROVISIONING = (
    "owner is assigned through provisioning, not the API: run "
    "scripts/host_ops/set_schedule_owner.py --reason <why> on the gateway host"
)


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

    @field_validator("owner_agent_id")
    @classmethod
    def _refuse_owner(cls, _value: int | None) -> None:
        raise ValueError(OWNER_VIA_PROVISIONING)


class ScheduleUpdate(BaseModel):
    name: str | None = None
    script: str | None = None
    command: str | None = None
    description: str | None = None
    enabled: bool | None = None
    machine: str | None = None
    owner_agent_id: int | None = None

    @field_validator("owner_agent_id")
    @classmethod
    def _refuse_owner(cls, _value: int | None) -> None:
        raise ValueError(OWNER_VIA_PROVISIONING)


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
