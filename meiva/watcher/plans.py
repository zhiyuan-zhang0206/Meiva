"""What each public watcher call asks for, as plain values.

`launch`, `cron` and `at` in `meiva/watcher/__init__.py` coerce their arguments and read the
clock and the configured timezones; the functions here turn those into a `SpawnRequest`. They
take the current time and the timezone defaults as arguments, so a test fixes both and asserts
on the request without starting a session.
"""

from __future__ import annotations

import datetime

from base.daemon.schedules.watcher import (
    DEFAULT_STANDING_CRON_MAX_SECONDS,
    build_at_script,
    build_cron_script,
    normalize_when,
    parse_timeout,
    session_deadline,
    validate_cron,
    validate_timezone,
)
from meiva.sdk_surface.validation import coerce_str
from meiva.shell.background_plan import notify_policy
from meiva.watcher.launch_plan import SpawnRequest

__all__ = ["plan_at", "plan_cron", "plan_launch"]


def validate_message(message: str) -> None:
    if not message.strip():
        raise ValueError("message cannot be empty")


# meiva:note
# The session lifetime of a watcher in seconds: its target deadline minus `now`. The deadline
# is derived once, by `base.daemon.schedules.watcher.session_deadline` (launch = created +
# timeout, cron = its end, at = its fire moment + grace). A missing or already-passed deadline is
# a call-site bug, never a reason to mount a stillborn session.
def _ttl_seconds(kind: str, deadline: datetime.datetime | None, now: datetime.datetime) -> float:
    if deadline is None or deadline <= now:
        raise ValueError(
            f"watcher {kind!r} target deadline is not in the future ({deadline!r}) - "
            "refusing to spawn its session"
        )
    return (deadline - now).total_seconds()


def plan_launch(
    code: str,
    timeout: float | datetime.timedelta | str,
    name: str,
    notify: str | None,
    now: datetime.datetime,
) -> SpawnRequest:
    """A background program bounded by `timeout`; exits with code 124 when it runs out."""
    code = coerce_str(code, "code")
    timeout = coerce_str(timeout, "timeout", allow_types=(int, float, datetime.timedelta))
    name = coerce_str(name, "name")
    policy = notify_policy(coerce_str(notify, "notify", allow_none=True))
    seconds = parse_timeout(timeout)
    return SpawnRequest(
        code=code,
        watchdog_secs=seconds,
        name=name,
        ttl_seconds=_ttl_seconds(
            "launch", session_deadline("launch", created_at=now, timeout_secs=seconds), now
        ),
        notify=policy,
    )


def plan_cron(
    expr: str,
    message: str,
    *,
    timezone: str | None,
    end_time: datetime.datetime | datetime.timedelta | str | None,
    name: str,
    notify: str | None,
    now: datetime.datetime,
    default_timezone: str,
) -> SpawnRequest:
    """A cron wake-up that runs until `end_time` (default: seven days from the current minute).

    `default_timezone` is the cluster clock's zone, or this host's zone for a settings-lite
    process; it applies when `timezone` is None.

    Raises:
        ValueError: the message is empty, or `end_time` is in the past.
    """
    expr = coerce_str(expr, "expr")
    message = coerce_str(message, "message")
    timezone = coerce_str(timezone, "timezone", allow_none=True)
    end_time = coerce_str(
        end_time, "end_time", allow_none=True, allow_types=(datetime.datetime, datetime.timedelta)
    )
    name = coerce_str(name, "name")
    policy = notify_policy(coerce_str(notify, "notify", allow_none=True))
    validate_message(message)
    validate_cron(expr)
    tz = timezone if timezone is not None else default_timezone
    validate_timezone(tz)
    if end_time is None:
        # Standing-cron cap (task #2617): no end_time means the DEFAULT
        # window - 7 days, counted from the current minute - not forever. A
        # longer schedule must pass an explicit end_time.
        et = now.replace(second=0, microsecond=0) + datetime.timedelta(
            seconds=DEFAULT_STANDING_CRON_MAX_SECONDS
        )
    else:
        # normalize_when (not normalize_end_time): this branch excludes None,
        # so the result is a non-optional datetime for the type checker.
        et = normalize_when(end_time)
        # Align with at(): a past end must not register.
        if et < now:
            raise ValueError(
                f"end_time is in the past: {et.isoformat()}. "
                "Provide a future time, or use a positive timedelta."
            )
    code = build_cron_script(
        expr=expr,
        message=message,
        timezone=tz,
        end_time_iso=et.isoformat(),
    )
    # The generated script self-terminates (it stops looping past end_time -
    # which every registration now carries, the default being now + 7 days),
    # so no watchdog.
    return SpawnRequest(
        code=code,
        watchdog_secs=None,
        name=name,
        ttl_seconds=_ttl_seconds("cron", session_deadline("cron", cron_end_at=et), now),
        notify=policy,
    )


def plan_at(
    when: datetime.datetime | datetime.timedelta | str,
    message: str,
    *,
    name: str,
    notify: str | None,
    now: datetime.datetime,
    announce_timezone: str | None,
) -> SpawnRequest:
    """A one-shot wake-up at `when`.

    The announcement's wall clock follows `announce_timezone` (the cluster timezone; None for
    a settings-lite process, which renders in the watcher's own wall clock); the sleep is
    UTC-based regardless.

    Raises:
        ValueError: the message is empty, or `when` is in the past.
    """
    when = coerce_str(when, "when", allow_types=(datetime.datetime, datetime.timedelta))
    message = coerce_str(message, "message")
    name = coerce_str(name, "name")
    policy = notify_policy(coerce_str(notify, "notify", allow_none=True))
    validate_message(message)
    due_at = normalize_when(when)
    if due_at < now:
        raise ValueError(
            f"when is in the past: {due_at.isoformat()}. "
            "Provide a future time, or use a positive timedelta."
        )
    code = build_at_script(
        when_iso=due_at.isoformat(),
        message=message,
        timezone=announce_timezone,
    )
    # The one-shot script sleeps until `when`, wakes you once, and exits - it
    # ends itself, so no watchdog.
    return SpawnRequest(
        code=code,
        watchdog_secs=None,
        name=name,
        ttl_seconds=_ttl_seconds("at", session_deadline("at", fires_at=due_at), now),
        notify=policy,
    )
