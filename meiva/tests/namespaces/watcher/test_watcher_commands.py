from __future__ import annotations

import datetime
from typing import Any

import pytest

from base.daemon.schedules.watcher import parse_timeout
from meiva import watcher
from meiva.shell.background_plan import NotifyPolicy
from meiva.watcher import plans


def test_validate_message_rejects_empty() -> None:
    with pytest.raises(ValueError, match="message cannot be empty"):
        watcher.at("2030-01-01T00:00:00Z", "   ", name="test-empty")


@pytest.mark.parametrize(
    "value, expected",
    [
        (90, 90.0),
        (datetime.timedelta(minutes=2), 120.0),
        ("30m", 1800.0),
        ("2h", 7200.0),
        ("1d", 86400.0),
        ("45s", 45.0),
    ],
)
def test_parse_timeout_accepts_forms(
    value: int | datetime.timedelta | str, expected: float
) -> None:
    assert parse_timeout(value) == expected


@pytest.mark.parametrize("bad", ["", "5x", "later", "-3"])
def test_parse_timeout_rejects_bad_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_timeout(bad)


def test_parse_timeout_rejects_nonpositive_and_bool() -> None:
    with pytest.raises(ValueError, match="positive"):
        parse_timeout(0)
    with pytest.raises(TypeError):
        parse_timeout(True)


_NOW = datetime.datetime(2026, 1, 1, 0, 0, 30, tzinfo=datetime.UTC)
_FAR = "2099-01-01T00:00:00Z"


def test_at_announcement_uses_cluster_zone_when_authoritative() -> None:
    request = plans.plan_at(
        _FAR,
        "ping later",
        name="test-at-cluster-tz",
        notify=None,
        now=_NOW,
        announce_timezone="Asia/Shanghai",
    )
    assert "_TZ = ZoneInfo('Asia/Shanghai')" in request.code
    assert "_WHEN.astimezone(_TZ).isoformat()" in request.code


def test_at_announcement_uses_host_clock_without_authoritative_zone() -> None:
    request = plans.plan_at(
        _FAR, "ping later", name="test-at-lite", notify=None, now=_NOW, announce_timezone=None
    )
    assert "ZoneInfo" not in request.code
    assert "_WHEN.astimezone().isoformat()" in request.code


def test_cron_defaults_to_the_given_default_zone() -> None:
    request = plans.plan_cron(
        "0 * * * *",
        "tick",
        timezone=None,
        end_time=None,
        name="test-cron-lite",
        notify=None,
        now=_NOW,
        default_timezone="Europe/Paris",
    )
    assert "_TZ = ZoneInfo('Europe/Paris')" in request.code
    assert "America/Los_Angeles" not in request.code


def _cron(expr: str = "0 3 * * *", **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "timezone": "UTC",
        "end_time": None,
        "name": "test-cron",
        "notify": None,
        "now": _NOW,
        "default_timezone": "UTC",
    }
    args.update(overrides)
    return plans.plan_cron(expr, "daily", **args)


def test_cron_invalid_expr_raises() -> None:
    from base.daemon.schedules.watcher import CronExprError

    with pytest.raises(CronExprError):
        _cron("not a cron")


def test_cron_invalid_timezone_raises() -> None:
    with pytest.raises(ValueError, match="timezone"):
        _cron(timezone="Not/A/Real/Timezone")


def test_at_past_time_raises() -> None:
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="past"):
        plans.plan_at(
            past, "too late", name="test-past", notify=None, now=_NOW, announce_timezone=None
        )


def test_cron_past_end_time_raises() -> None:
    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    with pytest.raises(ValueError, match="end_time is in the past"):
        _cron(end_time=past)


def test_cron_future_end_time_ok() -> None:
    end = _NOW + datetime.timedelta(days=2)
    request = _cron(end_time=end)
    assert request.ttl_seconds == datetime.timedelta(days=2).total_seconds()
    assert "daily" in request.code

    with pytest.raises(ValueError):
        plans.plan_at(
            datetime.timedelta(days=-1),
            "negative delta",
            name="test-neg-delta",
            notify=None,
            now=datetime.datetime.now(datetime.UTC),
            announce_timezone=None,
        )


def test_at_future_time_ok() -> None:
    far = plans.plan_at(
        "2099-01-01T00:00:00Z",
        "far future",
        name="test-future",
        notify=None,
        now=_NOW,
        announce_timezone=None,
    )
    assert "far future" in far.code
    assert far.name == "test-future"

    soon = plans.plan_at(
        datetime.timedelta(hours=1),
        "one hour",
        name="test-delta",
        notify=None,
        now=_NOW,
        announce_timezone=None,
    )
    assert "one hour" in soon.code


_FIXED_NOW = datetime.datetime(2026, 10, 24, 5, 6, 7, 500000, tzinfo=datetime.UTC)


def test_at_and_cron_plan_without_watchdog() -> None:
    # meiva:note
    # at/cron scripts self-terminate, so they plan with no watchdog (None).
    now = _FIXED_NOW
    when = (now + datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    at = plans.plan_at(
        when, "ping later", name="test-at", notify=None, now=now, announce_timezone=None
    )
    assert at.watchdog_secs is None
    assert "_wake(_MESSAGE)" in at.code
    assert "ping later" in at.code
    cron = plans.plan_cron(
        "0 3 * * *",
        "daily",
        timezone=None,
        end_time=None,
        name="test-cron",
        notify=None,
        now=now,
        default_timezone="UTC",
    )
    assert cron.watchdog_secs is None


def test_the_session_lifetime_is_each_watchers_target_deadline() -> None:
    # meiva:note
    # Task #3411: launch = created + timeout, cron = its end (default: the current minute + 7
    # days), at = its fire moment + the grace that lets the wake delivery finish.
    from base.daemon.schedules.watcher import AT_SESSION_TTL_GRACE_SECONDS

    now = _FIXED_NOW
    launch = plans.plan_launch("pass", "30m", "test-launch-ttl", None, now)
    cron = plans.plan_cron(
        "0 3 * * *",
        "daily",
        timezone=None,
        end_time=None,
        name="test-cron-ttl",
        notify=None,
        now=now,
        default_timezone="UTC",
    )
    explicit = plans.plan_cron(
        "0 3 * * *",
        "daily",
        timezone=None,
        end_time=now + datetime.timedelta(hours=30),
        name="test-cron-ttl-explicit",
        notify=None,
        now=now,
        default_timezone="UTC",
    )
    at = plans.plan_at(
        now + datetime.timedelta(hours=2),
        "ping later",
        name="test-at-ttl",
        notify=None,
        now=now,
        announce_timezone=None,
    )

    assert launch.ttl_seconds == 1800.0
    assert launch.watchdog_secs == 1800.0
    assert cron.ttl_seconds == datetime.timedelta(days=7).total_seconds() - 7.5
    assert explicit.ttl_seconds == datetime.timedelta(hours=30).total_seconds()
    assert at.ttl_seconds == 7200.0 + AT_SESSION_TTL_GRACE_SECONDS


def test_a_watcher_whose_deadline_has_passed_is_refused() -> None:
    now = _FIXED_NOW
    with pytest.raises(ValueError, match="in the past"):
        plans.plan_at(
            now - datetime.timedelta(seconds=1),
            "late",
            name="test-late",
            notify=None,
            now=now,
            announce_timezone=None,
        )


def test_watcher_plans_forward_failure_notify() -> None:
    now = datetime.datetime.now(datetime.UTC)
    when = (now + datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    requests = [
        plans.plan_launch("pass", "1h", "test-launch-notify", "always", now),
        plans.plan_at(
            when,
            "ping later",
            name="test-at-notify",
            notify="always",
            now=now,
            announce_timezone=None,
        ),
        plans.plan_cron(
            "0 3 * * *",
            "daily",
            timezone=None,
            end_time=None,
            name="test-cron-notify",
            notify="always",
            now=now,
            default_timezone="UTC",
        ),
    ]
    assert [r.notify for r in requests] == [NotifyPolicy.ALWAYS] * 3


def test_watcher_plans_refuse_an_unknown_notify_before_anything_starts() -> None:
    now = datetime.datetime.now(datetime.UTC)
    when = (now + datetime.timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    refused = "notify must be one of"

    with pytest.raises(ValueError, match=refused):
        plans.plan_launch("pass", "1h", "test-launch-notify", "success", now)
    with pytest.raises(ValueError, match=refused):
        plans.plan_at(
            when,
            "ping later",
            name="test-at-notify",
            notify="success",
            now=now,
            announce_timezone=None,
        )
    with pytest.raises(ValueError, match=refused):
        plans.plan_cron(
            "0 3 * * *",
            "daily",
            timezone=None,
            end_time=None,
            name="test-cron-notify",
            notify="success",
            now=now,
            default_timezone="UTC",
        )
