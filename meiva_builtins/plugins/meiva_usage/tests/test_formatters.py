"""Unit tests for meiva_usage card formatters."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from meiva_builtins.plugins.meiva_usage.formatters import (
    format_codex_window_card,
    format_deepseek_balance_card,
    format_dual_window_card,
)
from meiva_builtins.plugins.meiva_usage.types import RateWindow, UsageProbeError


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
    # session: 80% left; weekly: 40% left -> tightest is 40% left
    reading = format_dual_window_card(session, weekly)
    assert reading.value == "40% left"
    assert "5h 80% left" in reading.detail
    assert "7d 40% left" in reading.detail
    assert "resets 11-07 00:00" in reading.detail
    assert reading.status == "ok"


def test_format_dual_window_card_warn_threshold() -> None:
    session = RateWindow(used_percent=91.0)
    weekly = RateWindow(used_percent=20.0)
    # tightest is session: 9% left <= 10.0% -> status="warn"
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
