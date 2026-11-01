from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from meiva_builtins.plugins.meiva_usage.formatters import (
    format_admin_cost_card,
    format_antigravity_group_card,
    format_codex_window_card,
    format_deepseek_balance_card,
    format_dual_window_card,
)
from meiva_builtins.plugins.meiva_usage.types import (
    RateWindow,
    UsageProbeError,
    WindowPair,
)


def test_format_dual_window_card_both_windows() -> None:
    session = RateWindow(
        used_percent=20.0,
        resets_at=datetime(2026, 11, 1, 15, 30, tzinfo=UTC),
        window_minutes=300,
    )
    weekly = RateWindow(
        used_percent=60.0,
        resets_at=datetime(2026, 11, 7, 0, 0, tzinfo=UTC),
        window_minutes=10080,
    )
    reading = format_dual_window_card(session, weekly)
    assert reading.value == "40% left"
    assert "5h 80% left" in reading.detail
    assert "7d 40% left" in reading.detail
    assert "resets 11-07 00:00" in reading.detail
    assert reading.status == "ok"


def test_format_dual_window_card_warn_threshold() -> None:
    session = RateWindow(used_percent=91.0)
    weekly = RateWindow(used_percent=20.0)
    reading = format_dual_window_card(session, weekly)
    assert reading.value == "9% left"
    assert reading.status == "warn"


def test_format_dual_window_card_timezone_conversion() -> None:
    session = RateWindow(
        used_percent=50.0,
        resets_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )
    tz_shanghai = ZoneInfo("Asia/Shanghai")
    reading = format_dual_window_card(session, None, display_tz=tz_shanghai)
    assert reading.value == "50% left"
    assert "resets 11-01 20:00" in reading.detail


def test_format_dual_window_card_both_none_raises() -> None:
    with pytest.raises(UsageProbeError, match="missing rate-limit windows"):
        format_dual_window_card(None, None)


def test_format_antigravity_group_card_both_windows_healthy() -> None:
    session = RateWindow(
        used_percent=23.6813,
        resets_at=datetime(2026, 11, 1, 18, 0, tzinfo=UTC),
        window_minutes=300,
    )
    weekly = RateWindow(
        used_percent=18.4308,
        resets_at=datetime(2026, 11, 7, 0, 0, tzinfo=UTC),
        window_minutes=10080,
    )
    reading = format_antigravity_group_card(WindowPair(session=session, weekly=weekly))
    assert reading.value == "7d 82% · 5h 76% left"
    assert reading.detail == "resets 7d 11-07 00:00 · 5h 11-01 18:00"
    assert reading.status == "ok"


def test_format_antigravity_group_card_warn_threshold() -> None:
    session = RateWindow(
        used_percent=94.3613,
        resets_at=datetime(2026, 11, 1, 17, 0, tzinfo=UTC),
    )
    weekly = RateWindow(
        used_percent=99.097,
        resets_at=datetime(2026, 11, 8, 0, 0, tzinfo=UTC),
    )
    reading = format_antigravity_group_card(WindowPair(session=session, weekly=weekly))
    assert reading.value == "7d 1% · 5h 6% left"
    assert reading.detail == "resets 7d 11-08 00:00 · 5h 11-01 17:00"
    assert reading.status == "warn"

    r_weekly_low = format_antigravity_group_card(
        WindowPair(
            session=RateWindow(used_percent=20.0),
            weekly=RateWindow(used_percent=92.0),
        )
    )
    assert r_weekly_low.status == "warn"

    r_session_low = format_antigravity_group_card(
        WindowPair(
            session=RateWindow(used_percent=95.0),
            weekly=RateWindow(used_percent=20.0),
        )
    )
    assert r_session_low.status == "warn"


def test_format_antigravity_group_card_no_resets_fallback_detail() -> None:
    reading = format_antigravity_group_card(
        WindowPair(
            session=RateWindow(used_percent=30.0),
            weekly=RateWindow(used_percent=40.0),
        )
    )
    assert reading.value == "7d 60% · 5h 70% left"
    assert reading.detail == "7d & 5h windows"
    assert reading.status == "ok"


def test_format_antigravity_group_card_single_window_fallback() -> None:
    weekly_only = format_antigravity_group_card(
        WindowPair(
            weekly=RateWindow(
                used_percent=25.0,
                resets_at=datetime(2026, 11, 7, 0, 0, tzinfo=UTC),
            )
        )
    )
    assert weekly_only.value == "75% left"
    assert weekly_only.detail == "7d window · resets 11-07 00:00"
    assert weekly_only.status == "ok"

    session_only = format_antigravity_group_card(
        WindowPair(
            session=RateWindow(
                used_percent=95.0,
            )
        )
    )
    assert session_only.value == "5% left"
    assert session_only.detail == "5h window"
    assert session_only.status == "warn"


def test_format_antigravity_group_card_timezone_conversion() -> None:
    pair = WindowPair(
        session=RateWindow(
            used_percent=20.0,
            resets_at=datetime(2026, 11, 1, 6, 0, tzinfo=UTC),
        ),
        weekly=RateWindow(
            used_percent=20.0,
            resets_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
        ),
    )
    tz_shanghai = ZoneInfo("Asia/Shanghai")
    reading = format_antigravity_group_card(pair, display_tz=tz_shanghai)
    assert reading.value == "7d 80% · 5h 80% left"
    assert reading.detail == "resets 7d 11-01 20:00 · 5h 11-01 14:00"


def test_format_antigravity_group_card_empty_raises() -> None:
    with pytest.raises(UsageProbeError, match="missing rate-limit windows"):
        format_antigravity_group_card(WindowPair())


def test_format_codex_window_card_labels() -> None:
    cases = [
        (10080, "7d window"),
        (1440, "1d window"),
        (300, "5h window"),
        (120, "2h window"),
        (45, "45m window"),
        (None, None),
    ]
    for minutes, expected_label in cases:
        win = RateWindow(
            used_percent=35.0,
            resets_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
            window_minutes=minutes,
        )
        reading = format_codex_window_card(win)
        assert reading.value == "65% left"
        assert "used 35%" in reading.detail
        assert "resets 11-01 10:00" in reading.detail
        if expected_label:
            assert expected_label in reading.detail
        assert reading.status == "ok"


def test_format_codex_window_card_warn_threshold() -> None:
    win = RateWindow(used_percent=95.0)
    reading = format_codex_window_card(win)
    assert reading.value == "5% left"
    assert reading.status == "warn"


def test_format_deepseek_balance_card_cny_preferred() -> None:
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "USD", "total_balance": "10.00"},
            {"currency": "CNY", "total_balance": "42.50"},
        ],
    }
    reading = format_deepseek_balance_card(payload)
    assert reading.value == "CNY 42.50"
    assert reading.detail == "USD 10.00"
    assert reading.status == "ok"


def test_format_deepseek_balance_card_single_currency() -> None:
    payload = {
        "is_available": True,
        "balance_infos": [
            {"currency": "CNY", "total_balance": "100.00"},
        ],
    }
    reading = format_deepseek_balance_card(payload)
    assert reading.value == "CNY 100.00"
    assert reading.detail == "platform.deepseek.com"
    assert reading.status == "ok"


def test_format_deepseek_balance_card_unavailable_warn() -> None:
    payload = {
        "is_available": False,
        "balance_infos": [
            {"currency": "USD", "total_balance": "0.00"},
        ],
    }
    reading = format_deepseek_balance_card(payload)
    assert reading.value == "USD 0.00"
    assert reading.status == "warn"


def test_format_deepseek_balance_card_missing_info_raises() -> None:
    with pytest.raises(UsageProbeError, match="missing balance_infos"):
        format_deepseek_balance_card({})

    with pytest.raises(UsageProbeError, match="missing balance_infos"):
        format_deepseek_balance_card({"balance_infos": []})


def test_format_admin_cost_card_trailing_30d() -> None:
    today = date(2026, 11, 1)
    daily = {
        today: Decimal("2.50"),
        date(2026, 10, 15): Decimal("10.00"),
        date(2026, 10, 3): Decimal("5.00"),
        date(2026, 10, 2): Decimal("100.00"),
        date(2026, 11, 2): Decimal("50.00"),
    }
    reading = format_admin_cost_card(daily, today_utc=today)
    assert reading.value == "$17.50 (30d)"
    assert reading.detail == "today $2.50 · 30d $17.50"
    assert reading.status == "ok"


def test_format_admin_cost_card_trailing_30d_zero_spend() -> None:
    today = date(2026, 11, 1)
    reading = format_admin_cost_card({}, today_utc=today)
    assert reading.value == "$0.00 (30d)"
    assert reading.detail == "today $0.00 · 30d $0.00"
    assert reading.status == "ok"


def test_format_admin_cost_card_prepaid_balance_ok() -> None:
    today = date(2026, 11, 1)
    as_of = date(2026, 10, 15)
    daily = {
        date(2026, 10, 10): Decimal("20.00"),
        date(2026, 10, 15): Decimal("10.00"),
        today: Decimal("5.00"),
    }
    reading = format_admin_cost_card(
        daily,
        today_utc=today,
        known_balance_usd=100.0,
        balance_as_of=as_of,
    )
    assert reading.value == "$85.00 left"
    assert reading.detail == "today $5.00 · spent $15.00 since 2026-10-15"
    assert reading.status == "ok"


def test_format_admin_cost_card_warn_threshold_percentage() -> None:
    today = date(2026, 11, 1)
    as_of = date(2026, 10, 1)

    r_warn = format_admin_cost_card(
        {today: Decimal("90.00")},
        today_utc=today,
        known_balance_usd=100.0,
        balance_as_of=as_of,
    )
    assert r_warn.value == "$10.00 left"
    assert r_warn.status == "warn"

    r_ok = format_admin_cost_card(
        {today: Decimal("89.99")},
        today_utc=today,
        known_balance_usd=100.0,
        balance_as_of=as_of,
    )
    assert r_ok.value == "$10.01 left"
    assert r_ok.status == "ok"


def test_format_admin_cost_card_warn_threshold_small_and_negative() -> None:
    today = date(2026, 11, 1)
    as_of = date(2026, 10, 1)

    r_warn_small = format_admin_cost_card(
        {today: Decimal("15.00")},
        today_utc=today,
        known_balance_usd=20.0,
        balance_as_of=as_of,
    )
    assert r_warn_small.value == "$5.00 left"
    assert r_warn_small.status == "warn"

    r_ok_small = format_admin_cost_card(
        {today: Decimal("14.99")},
        today_utc=today,
        known_balance_usd=20.0,
        balance_as_of=as_of,
    )
    assert r_ok_small.value == "$5.01 left"
    assert r_ok_small.status == "ok"

    r_neg = format_admin_cost_card(
        {today: Decimal("60.00")},
        today_utc=today,
        known_balance_usd=50.0,
        balance_as_of=as_of,
    )
    assert r_neg.value == "$-10.00 left"
    assert r_neg.status == "warn"


def test_format_admin_cost_card_validation_errors() -> None:
    today = date(2026, 11, 1)
    with pytest.raises(ValueError, match="must be configured together"):
        format_admin_cost_card({}, today_utc=today, known_balance_usd=100.0, balance_as_of=None)

    with pytest.raises(ValueError, match="must be configured together"):
        format_admin_cost_card({}, today_utc=today, known_balance_usd=None, balance_as_of=today)
