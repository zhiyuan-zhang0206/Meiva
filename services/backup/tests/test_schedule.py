from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from services.backup.schedule import (
    backup_due,
    restore_due,
    restore_window,
    seconds_until_next_backup_hour,
)

_LA = ZoneInfo("America/Los_Angeles")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HOUR = 3


def _la(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=_LA)


def test_not_due_before_the_backup_hour() -> None:
    assert not backup_due(_la(10, _HOUR - 1, 59), _LA, _HOUR, None)


def test_due_at_the_hour_with_no_dumps() -> None:
    assert backup_due(_la(10, _HOUR), _LA, _HOUR, None)


def test_not_due_again_after_todays_dump() -> None:
    # meiva:note
    # 03:00 PDT
    todays = datetime(2026, 6, 10, 10, 0, 1, tzinfo=UTC)

    assert not backup_due(_la(10, 9), _LA, _HOUR, todays)


def test_due_again_the_next_day() -> None:
    yesterdays = datetime(2026, 6, 9, 10, 0, 1, tzinfo=UTC)

    assert backup_due(_la(10, _HOUR), _LA, _HOUR, yesterdays)


# meiva:note
# Host down at 03:00 -> the first wake later that day is still due.
def test_catch_up_after_downtime() -> None:
    yesterdays = datetime(2026, 6, 9, 10, 0, 1, tzinfo=UTC)

    assert backup_due(_la(10, 14, 30), _LA, _HOUR, yesterdays)


def test_the_due_hour_is_an_argument() -> None:
    assert not backup_due(_la(10, 1, 59), _LA, 2, None)
    assert backup_due(_la(10, 2), _LA, 2, None)


def test_a_naive_instant_is_rejected_rather_than_read_as_host_local() -> None:
    with pytest.raises(ValueError, match="TZ-aware"):
        backup_due(datetime.fromisoformat("2026-06-10T03:00:00"), _LA, _HOUR, None)


# meiva:note
# One instant, several host clocks, one verdict.
#
# Due-ness once ran on the host's wall clock, so carrying a laptop from Asia/Shanghai
# to US/Pacific put the newest dump's local date *ahead* of the new local date and the
# daily backup was skipped silently.
def test_the_day_boundary_is_cluster_time_not_host_time() -> None:
    # meiva:note
    # 03:00 Jun-10 Shanghai
    newest = datetime(2026, 6, 9, 19, 0, tzinfo=UTC)
    # meiva:note
    # 04:00 Jun-10 Shanghai
    today = datetime(2026, 6, 9, 20, 0, tzinfo=UTC)
    # meiva:note
    # 04:00 Jun-11 Shanghai
    tomorrow = datetime(2026, 6, 10, 20, 0, tzinfo=UTC)

    for host_tz in ("Asia/Shanghai", "America/Los_Angeles", "Pacific/Kiritimati"):
        assert not backup_due(today.astimezone(ZoneInfo(host_tz)), _SHANGHAI, _HOUR, newest)
        assert backup_due(tomorrow.astimezone(ZoneInfo(host_tz)), _SHANGHAI, _HOUR, newest)


def test_the_backup_hour_is_cluster_time_whatever_the_host_says() -> None:
    assert not backup_due(datetime(2026, 6, 9, 18, 59, tzinfo=UTC), _SHANGHAI, _HOUR, None)
    assert backup_due(datetime(2026, 6, 9, 19, 0, tzinfo=UTC), _SHANGHAI, _HOUR, None)


def test_the_next_backup_hour_uses_the_cluster_timezone() -> None:
    now = datetime(2026, 8, 25, _HOUR - 1, 30, tzinfo=UTC)

    assert seconds_until_next_backup_hour(now, ZoneInfo("UTC"), _HOUR) == 30 * 60


def test_after_the_backup_hour_the_next_one_is_tomorrows() -> None:
    now = datetime(2026, 8, 25, _HOUR, 0, tzinfo=UTC)

    assert seconds_until_next_backup_hour(now, ZoneInfo("UTC"), _HOUR) == 24 * 3600


# meiva:note
# Sleeping from a job's completion, not from its start, lands on the next hour.
def test_the_wait_is_measured_from_the_instant_it_is_asked() -> None:
    started = datetime(2026, 8, 25, _HOUR, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 25, _HOUR, 53, 52, tzinfo=UTC)

    assert seconds_until_next_backup_hour(started, ZoneInfo("UTC"), _HOUR) == 24 * 3600
    assert seconds_until_next_backup_hour(finished, ZoneInfo("UTC"), _HOUR) == (
        24 * 3600 - 53 * 60 - 52
    )


def test_the_wait_follows_the_hour_it_is_given() -> None:
    now = datetime(2026, 8, 25, 6, 30, tzinfo=UTC)

    assert seconds_until_next_backup_hour(now, ZoneInfo("UTC"), 7) == 1800
    assert seconds_until_next_backup_hour(now, ZoneInfo("UTC"), 8) == 5400


# meiva:note
# Los Angeles springs forward on 2026-03-08: 03:00 on that day is 23 hours after 03:00 on the 7th.
def test_the_wait_across_a_dst_change_is_real_elapsed_time() -> None:
    now = datetime(2026, 3, 7, 3, 0, tzinfo=_LA)

    assert seconds_until_next_backup_hour(now, _LA, _HOUR) == 23 * 3600


def test_a_naive_instant_cannot_ask_for_a_wait() -> None:
    with pytest.raises(ValueError, match="TZ-aware"):
        seconds_until_next_backup_hour(
            datetime.fromisoformat("2026-08-25T03:00:00"), ZoneInfo("UTC"), _HOUR
        )


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def test_the_restore_window_is_the_latest_sunday_at_the_hour() -> None:
    utc = ZoneInfo("UTC")

    assert restore_window(_utc(30, 3), utc, 3) == _utc(30, 3)
    assert restore_window(_utc(30, 2, 59), utc, 3) == _utc(23, 3)
    assert restore_window(_utc(26, 12), utc, 3) == _utc(23, 3)


def test_the_restore_runs_once_after_the_weekly_window() -> None:
    utc = ZoneInfo("UTC")
    window = _utc(30, 3)
    prior_success = _utc(23, 3)

    assert not restore_due(_utc(30, 2, 59), utc, 3, prior_success)
    assert restore_due(window, utc, 3, prior_success)
    assert not restore_due(window, utc, 3, window)
    assert restore_due(datetime(2026, 9, 6, 3, tzinfo=UTC), utc, 3, window)
    assert restore_due(window, utc, 3, None)


def test_a_naive_instant_cannot_ask_for_a_restore() -> None:
    with pytest.raises(ValueError, match="TZ-aware"):
        restore_due(datetime.fromisoformat("2026-08-30T03:00:00"), ZoneInfo("UTC"), 3, None)
