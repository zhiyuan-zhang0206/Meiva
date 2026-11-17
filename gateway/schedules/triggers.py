# meiva:note
# Pure validation and timing decisions for declarative schedule mutations.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from base.daemon.schedules.watcher import next_fire, validate_cron, validate_timezone
from gateway.schedules.contracts import ScheduleCreate
from gateway.schedules.rows import ControlRow, OwnerTermination


@dataclass(frozen=True)
class PreparedSchedule:
    script: str
    timezone: str
    next_launch_at: datetime | None
    on_owner_terminate: OwnerTermination


# meiva:note
# A wake-only schedule remains an ordinary independently executable Python payload.
def message_script(target_agent_id: int, message: str) -> str:
    return f"import meiva\n\nmeiva.agents.send_message({target_agent_id!r}, {message!r})\n"


def prepare(body: ScheduleCreate, *, cluster_timezone: str, now: datetime) -> PreparedSchedule:
    # meiva:note
    # ScheduleCreate's closed trigger forms prove each asserted value before this pure timing step.
    timezone = body.timezone or cluster_timezone
    validate_timezone(timezone)
    normalized_now = now.astimezone(UTC)
    if body.trigger_kind == "at":
        assert body.run_at is not None  # noqa: S101
        next_launch_at = body.run_at.astimezone(UTC)
    elif body.trigger_kind == "cron":
        assert body.cron_expr is not None  # noqa: S101
        validate_cron(body.cron_expr, now=normalized_now)
        next_launch_at = next_fire(body.cron_expr, normalized_now, timezone)
    else:
        assert body.interval_seconds is not None  # noqa: S101
        next_launch_at = normalized_now + timedelta(seconds=body.interval_seconds)
    if not body.enabled:
        next_launch_at = None
    target = body.target_agent_id if body.target_agent_id is not None else body.owner_agent_id
    script = body.script
    if script is None:
        assert target is not None and body.message is not None  # noqa: S101
        script = message_script(target, body.message)
    return PreparedSchedule(
        script=script,
        timezone=timezone,
        next_launch_at=next_launch_at,
        on_owner_terminate=body.on_owner_terminate
        or ("cancel" if body.owner_agent_id is not None else "keep"),
    )


def enabled_next_launch(row: ControlRow, now: datetime) -> datetime:
    normalized_now = now.astimezone(UTC)
    if row.trigger_kind == "at":
        if row.run_at is None:
            raise ValueError("at schedule has no run_at")
        return max(row.run_at, normalized_now)
    if row.trigger_kind == "cron":
        if row.cron_expr is None:
            raise ValueError("cron schedule has no cron_expr")
        return next_fire(row.cron_expr, normalized_now, row.timezone)
    if row.trigger_kind == "every":
        if row.interval_seconds is None:
            raise ValueError("every schedule has no interval_seconds")
        return normalized_now + timedelta(seconds=row.interval_seconds)
    raise ValueError(f"unsupported schedule trigger: {row.trigger_kind!r}")
