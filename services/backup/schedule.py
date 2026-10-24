# meiva:note
# When the scheduled backup work is due, as values in and values out.
#
# The scheduler daemon owns the loop, the clock and the files. This module owns
# every calendar decision: whether the daily dump is due, how long to sleep until
# the next backup hour, and whether this week's logical restore drill still lacks a
# success. Every decision is made on the cluster's wall clock, the `tz` argument,
# never on the host's; a naive instant is refused rather than read as host-local.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

__all__ = [
    "backup_due",
    "restore_due",
    "restore_window",
    "seconds_until_next_backup_hour",
]

_WEEKLY_RESTORE_WEEKDAY = 6


# meiva:note
# A naive datetime is rejected rather than read as host-local: silently adopting the
# host's timezone is the exact failure this module replaced.
def _aware(now: datetime, what: str) -> datetime:
    if now.tzinfo is None:
        raise ValueError(f"{what} needs a TZ-aware datetime, got naive {now!r}")
    return now


# meiva:note
# True once the cluster clock has passed `hour` with no dump for the current cluster
# day. `newest` is the instant of the newest managed dump, if any.
def backup_due(now: datetime, tz: ZoneInfo, hour: int, newest: datetime | None) -> bool:
    local_now = _aware(now, "backup").astimezone(tz)
    if local_now.hour < hour:
        return False
    return newest is None or newest.astimezone(tz).date() < local_now.date()


# meiva:note
# Seconds from `now` to the next backup hour on the cluster clock (the same day when
# the hour is still ahead, otherwise tomorrow's).
def seconds_until_next_backup_hour(now: datetime, tz: ZoneInfo, hour: int) -> float:
    local_now = _aware(now, "backup").astimezone(tz)
    target = datetime.combine(local_now.date(), clock_time(hour=hour), tzinfo=local_now.tzinfo)
    if local_now >= target:
        target = datetime.combine(
            local_now.date() + timedelta(days=1), clock_time(hour=hour), tzinfo=local_now.tzinfo
        )
    return (target.astimezone(UTC) - now).total_seconds()


# meiva:note
# The start of the current weekly restore window: the latest Sunday at `hour` on the
# cluster clock that is not after `now`, in UTC.
def restore_window(now: datetime, tz: ZoneInfo, hour: int) -> datetime:
    local_now = now.astimezone(tz)
    days_since_sunday = (local_now.weekday() - _WEEKLY_RESTORE_WEEKDAY) % 7
    scheduled = datetime.combine(
        local_now.date() - timedelta(days=days_since_sunday), clock_time(hour=hour), tzinfo=tz
    )
    if scheduled > local_now:
        scheduled -= timedelta(days=7)
    return scheduled.astimezone(UTC)


# meiva:note
# Whether this week's post-backup restore window lacks a success record.
def restore_due(now: datetime, tz: ZoneInfo, hour: int, last_success: datetime | None) -> bool:
    scheduled = restore_window(_aware(now, "recovery drill"), tz, hour)
    if now < scheduled:
        return False
    return last_success is None or last_success < scheduled
