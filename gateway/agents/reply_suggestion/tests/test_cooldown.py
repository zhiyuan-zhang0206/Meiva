from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gateway.agents.reply_suggestion.cooldown import WarningCooldown

_T0 = datetime(2026, 11, 11, tzinfo=UTC)


def test_the_first_occurrence_warns_and_repeats_in_the_window_are_counted() -> None:
    cooldown = WarningCooldown(timedelta(minutes=5))

    assert cooldown.admit("draft:m", _T0) == 0
    assert cooldown.admit("draft:m", _T0 + timedelta(minutes=1)) is None
    assert cooldown.admit("draft:m", _T0 + timedelta(minutes=4)) is None
    assert cooldown.admit("draft:m", _T0 + timedelta(minutes=5)) == 2
    assert cooldown.admit("draft:m", _T0 + timedelta(minutes=6)) is None


def test_keys_are_independent() -> None:
    cooldown = WarningCooldown(timedelta(minutes=5))

    assert cooldown.admit("gate:a", _T0) == 0
    assert cooldown.admit("draft:b", _T0) == 0
