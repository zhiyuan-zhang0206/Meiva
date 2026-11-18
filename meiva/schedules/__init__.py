"""Declarative, run-to-completion schedules for ordinary Python payloads."""

from __future__ import annotations

import builtins
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

import meiva.sdk_surface.agent_identity
from meiva import gateway_client

type TriggerKind = Literal["at", "cron", "every"]
type Owner = int | Literal["self"] | None

__all_for_meiva__ = [
    "Schedule",
    "TriggerKind",
    "at",
    "cancel",
    "cron",
    "disable",
    "enable",
    "every",
    "get",
    "list",
    "run_now",
]


class Schedule(BaseModel):
    """The durable trigger, placement, lifecycle state, and payload of one schedule."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    description: str | None
    script: str | None = None
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    machine: str
    owner_agent_id: int | None
    trigger_kind: TriggerKind
    run_at: datetime | None
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    timeout_seconds: int
    on_owner_terminate: Literal["cancel", "keep"]
    next_launch_at: datetime | None


def _owner(value: Owner) -> int | None:
    if value != "self":
        return value
    current = meiva.sdk_surface.agent_identity.agent_id()
    if current is None:
        raise RuntimeError(
            "owner_agent_id='self' requires an agent context; pass None for a system schedule"
        )
    return current


def _create(
    *,
    name: str,
    trigger_kind: Literal["at", "cron", "every"],
    script: str | None,
    message: str | None,
    target_agent_id: int | None,
    run_at: datetime | None,
    cron_expr: str | None,
    interval_seconds: int | None,
    timezone: str | None,
    timeout_seconds: int,
    on_owner_terminate: Literal["cancel", "keep"] | None,
    description: str | None,
    enabled: bool,
    machine: str | None,
    owner_agent_id: Owner,
) -> Schedule:
    if (script is None) == (message is None):
        raise ValueError("exactly one of script or message is required")
    owner = _owner(owner_agent_id)
    if message is not None and target_agent_id is None and owner is None:
        raise ValueError("message requires target_agent_id or an owner agent")
    body: dict[str, object] = {
        "name": name,
        "trigger_kind": trigger_kind,
        "timeout_seconds": timeout_seconds,
        "enabled": enabled,
    }
    optional: dict[str, object | None] = {
        "script": script,
        "message": message,
        "target_agent_id": target_agent_id,
        "run_at": None if run_at is None else run_at.isoformat(),
        "cron_expr": cron_expr,
        "interval_seconds": interval_seconds,
        "timezone": timezone,
        "on_owner_terminate": on_owner_terminate,
        "description": description,
        "machine": machine,
        "owner_agent_id": owner,
    }
    body.update((key, value) for key, value in optional.items() if value is not None)
    return Schedule.model_validate(gateway_client.schedule_create(body))


def at(
    run_at: datetime,
    *,
    name: str,
    script: str | None = None,
    message: str | None = None,
    target_agent_id: int | None = None,
    timezone: str | None = None,
    timeout_seconds: int = 600,
    on_owner_terminate: Literal["cancel", "keep"] | None = None,
    description: str | None = None,
    enabled: bool = True,
    machine: str | None = None,
    owner_agent_id: Owner = "self",
) -> Schedule:
    """Create a one-shot schedule; a missed instant fires once after service recovery.

    The owner defaults to the current agent. Pass ``owner_agent_id=None`` for a
    system schedule, or an explicit agent id for another agent-owned schedule.
    """
    if run_at.utcoffset() is None:
        raise ValueError("run_at must be timezone-aware")
    return _create(
        name=name,
        trigger_kind="at",
        script=script,
        message=message,
        target_agent_id=target_agent_id,
        run_at=run_at,
        cron_expr=None,
        interval_seconds=None,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
        on_owner_terminate=on_owner_terminate,
        description=description,
        enabled=enabled,
        machine=machine,
        owner_agent_id=owner_agent_id,
    )


def cron(
    expression: str,
    *,
    name: str,
    script: str | None = None,
    message: str | None = None,
    target_agent_id: int | None = None,
    timezone: str | None = None,
    timeout_seconds: int = 600,
    on_owner_terminate: Literal["cancel", "keep"] | None = None,
    description: str | None = None,
    enabled: bool = True,
    machine: str | None = None,
    owner_agent_id: Owner = "self",
) -> Schedule:
    """Create a cron schedule whose fires never overlap or replay old missed slots.

    ``timezone`` defaults to the configured timezone. Ownership defaults to the
    current agent; pass ``owner_agent_id=None`` for a system schedule.
    """
    return _create(
        name=name,
        trigger_kind="cron",
        script=script,
        message=message,
        target_agent_id=target_agent_id,
        run_at=None,
        cron_expr=expression,
        interval_seconds=None,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
        on_owner_terminate=on_owner_terminate,
        description=description,
        enabled=enabled,
        machine=machine,
        owner_agent_id=owner_agent_id,
    )


def every(
    interval_seconds: int,
    *,
    name: str,
    script: str | None = None,
    message: str | None = None,
    target_agent_id: int | None = None,
    timezone: str | None = None,
    timeout_seconds: int = 600,
    on_owner_terminate: Literal["cancel", "keep"] | None = None,
    description: str | None = None,
    enabled: bool = True,
    machine: str | None = None,
    owner_agent_id: Owner = "self",
) -> Schedule:
    """Create a fixed-interval schedule, with at least 30 seconds between fires.

    The interval advances after completion. Ownership defaults to the current
    agent; pass ``owner_agent_id=None`` for a system schedule.
    """
    if interval_seconds < 30:
        raise ValueError("interval_seconds must be at least 30")
    return _create(
        name=name,
        trigger_kind="every",
        script=script,
        message=message,
        target_agent_id=target_agent_id,
        run_at=None,
        cron_expr=None,
        interval_seconds=interval_seconds,
        timezone=timezone,
        timeout_seconds=timeout_seconds,
        on_owner_terminate=on_owner_terminate,
        description=description,
        enabled=enabled,
        machine=machine,
        owner_agent_id=owner_agent_id,
    )


def list() -> builtins.list[Schedule]:
    """List all schedules, including completed, cancelled, and disabled rows."""
    return [Schedule.model_validate(row) for row in gateway_client.schedule_list()]


def get(schedule_id: int) -> Schedule:
    """Get one schedule by id."""
    return Schedule.model_validate(gateway_client.schedule_get(schedule_id))


def _action(schedule_id: int, action: str) -> Schedule:
    return Schedule.model_validate(gateway_client.schedule_action(schedule_id, action))


def cancel(schedule_id: int) -> Schedule:
    """Cancel unfinished work and prevent future fires."""
    return _action(schedule_id, "cancel")


def enable(schedule_id: int) -> Schedule:
    """Enable a schedule and calculate its next eligible fire."""
    return _action(schedule_id, "enable")


def disable(schedule_id: int) -> Schedule:
    """Disable future fires, terminating an active fire through manager reconciliation."""
    return _action(schedule_id, "disable")


def run_now(schedule_id: int) -> Schedule:
    """Enable an idle schedule and make one fire immediately eligible."""
    return _action(schedule_id, "run-now")
