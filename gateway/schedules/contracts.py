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
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

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
type TriggerKind = Literal["at", "cron", "every"]
type DeclarativeTriggerKind = Literal["at", "cron", "every"]


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
    trigger_kind: TriggerKind
    run_at: datetime | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = None
    timezone: str
    timeout_seconds: int
    on_owner_terminate: Literal["cancel", "keep"]
    next_launch_at: datetime | None = None
    placement_status: PlacementStatus
    placement_reason: str | None = None


class ScheduleView(ScheduleSummary):
    script: str


class ScheduleCreate(BaseModel):
    name: str
    trigger_kind: DeclarativeTriggerKind
    script: str | None = None
    message: str | None = None
    target_agent_id: int | None = None
    run_at: datetime | None = None
    cron_expr: str | None = None
    interval_seconds: int | None = Field(default=None, ge=30)
    timezone: str | None = None
    timeout_seconds: int = Field(default=600, gt=0)
    on_owner_terminate: Literal["cancel", "keep"] | None = None
    command: str = DEFAULT_COMMAND
    description: str | None = None
    enabled: bool = True
    machine: str | None = None
    owner_agent_id: int | None = None

    @model_validator(mode="after")
    def _validate_trigger(self) -> Self:
        _validate_payload(self)
        _validate_trigger_shape(self)
        return self


def _validate_payload(body: ScheduleCreate) -> None:
    if (body.script is None) == (body.message is None):
        raise ValueError("exactly one of script or message is required")
    if body.message is not None and body.target_agent_id is None and body.owner_agent_id is None:
        raise ValueError("message requires target_agent_id or owner_agent_id")


def _validate_trigger_shape(body: ScheduleCreate) -> None:
    if body.trigger_kind == "at":
        if body.run_at is None or body.run_at.utcoffset() is None:
            raise ValueError("at requires a timezone-aware run_at")
        if body.cron_expr is not None or body.interval_seconds is not None:
            raise ValueError("at accepts run_at only")
    elif body.trigger_kind == "cron":
        if not body.cron_expr or body.run_at is not None or body.interval_seconds is not None:
            raise ValueError("cron requires cron_expr only")
    elif body.interval_seconds is None or body.run_at is not None or body.cron_expr is not None:
        raise ValueError("every requires interval_seconds only")


class ScheduleUpdate(BaseModel):
    name: str | None = None
    script: str | None = None
    command: str | None = None
    description: str | None = None
    enabled: bool | None = None
    machine: str | None = None
    owner_agent_id: int | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    on_owner_terminate: Literal["cancel", "keep"] | None = None


class ScheduleRunView(BaseModel):
    id: int
    ran_at: datetime
    ok: bool | None
    agent_id: int | None
    note: str | None
    scheduled_for: datetime | None = None


class ScheduleLogsView(BaseModel):
    source: str
    lines: list[str]


class ScheduleDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language description of the scheduled task.")


class ScheduleDraftResponse(BaseModel):
    agent_id: int
