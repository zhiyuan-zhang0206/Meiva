# meiva:note
# Recurring trigger arithmetic lives below both daemon provisioning and plugin surfaces so every
# caller computes the same timezone-aware slot without importing daemon runtime code.
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

RecurringTriggerKind = Literal["cron", "every"]


class CronExprError(Exception):
    pass


def validate_timezone(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, KeyError) as exc:
        raise ValueError(f"invalid IANA timezone: {tz!r}") from exc


def validate_cron(expr: str, *, now: _dt.datetime) -> None:
    try:
        croniter(expr, now)
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc


def next_fire(
    expr: str,
    after: _dt.datetime,
    timezone: str | None = None,
    tolerance: _dt.timedelta | None = None,
) -> _dt.datetime:
    base = after
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)
    if tolerance is not None:
        base = base - tolerance

    compute_tz = ZoneInfo(timezone) if timezone else _dt.UTC
    base_in_tz = base.astimezone(compute_tz)
    try:
        iterator = croniter(expr, base_in_tz)
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc
    nxt: _dt.datetime = iterator.get_next(_dt.datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=compute_tz)
    return nxt.astimezone(_dt.UTC)


def previous_fire(
    expr: str,
    before: _dt.datetime,
    timezone: str | None = None,
) -> _dt.datetime:
    base = before
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.UTC)

    compute_tz = ZoneInfo(timezone) if timezone else _dt.UTC
    base_in_tz = base.astimezone(compute_tz)
    try:
        iterator = croniter(expr, base_in_tz)
    except (ValueError, KeyError) as exc:
        raise CronExprError(f"invalid cron expression: {expr!r}") from exc
    previous: _dt.datetime = iterator.get_prev(_dt.datetime)
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=compute_tz)
    return previous.astimezone(_dt.UTC)


@dataclass(frozen=True)
class RecurringTrigger:
    kind: RecurringTriggerKind
    cron_expr: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None
    timeout_seconds: int = 600

    def validate(self) -> None:
        if self.kind not in ("cron", "every"):
            raise ValueError("trigger kind must be 'cron' or 'every'")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.timezone is not None:
            validate_timezone(self.timezone)
        if self.kind == "cron":
            if not self.cron_expr or self.interval_seconds is not None:
                raise ValueError("cron trigger requires cron_expr only")
            validate_cron(self.cron_expr, now=_dt.datetime(2000, 1, 1, tzinfo=_dt.UTC))
            return
        if self.cron_expr is not None or self.interval_seconds is None:
            raise ValueError("every trigger requires interval_seconds only")
        if self.interval_seconds < 30:
            raise ValueError("interval_seconds must be at least 30")

    def next_launch(self, *, now: _dt.datetime, cluster_timezone: str) -> _dt.datetime:
        self.validate()
        normalized = now.astimezone(_dt.UTC)
        if self.kind == "cron":
            if self.cron_expr is None:
                raise AssertionError("validated cron trigger lost cron_expr")
            return next_fire(self.cron_expr, normalized, self.timezone or cluster_timezone)
        if self.interval_seconds is None:
            raise AssertionError("validated every trigger lost interval_seconds")
        return normalized + _dt.timedelta(seconds=self.interval_seconds)
