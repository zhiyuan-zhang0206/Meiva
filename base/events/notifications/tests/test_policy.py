from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from base.events.notifications.catalog import NotificationCategory
from base.events.notifications.policy import SuppressionReason, decide

NOW = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)


def _category(*, default_enabled: bool = True, window: int = 0) -> NotificationCategory:
    return NotificationCategory(
        id="plugin.event",
        label="Plugin event",
        description="A plugin declared its own category.",
        importance="normal",
        default_enabled=default_enabled,
        dedupe_window_seconds=window,
    )


def test_default_switch_delivers() -> None:
    decision = decide(_category(), overrides={}, last_same_key_at=None, now=NOW)
    assert decision.deliver
    assert decision.reason is None


def test_default_off_category_is_suppressed() -> None:
    decision = decide(
        _category(default_enabled=False), overrides={}, last_same_key_at=None, now=NOW
    )
    assert not decision.deliver
    assert decision.reason is SuppressionReason.DISABLED


def test_user_override_beats_the_default() -> None:
    on = decide(
        _category(default_enabled=False),
        overrides={"plugin.event": True},
        last_same_key_at=None,
        now=NOW,
    )
    off = decide(_category(), overrides={"plugin.event": False}, last_same_key_at=None, now=NOW)
    assert on.deliver
    assert off.reason is SuppressionReason.DISABLED


def test_non_bool_override_is_a_programming_error() -> None:
    bad = cast(dict[str, bool], {"plugin.event": "yes"})
    with pytest.raises(TypeError):
        decide(_category(), overrides=bad, last_same_key_at=None, now=NOW)


def test_key_inside_the_dedupe_window_is_suppressed() -> None:
    category = _category(window=600)
    decision = decide(
        category, overrides={}, last_same_key_at=NOW - timedelta(seconds=599), now=NOW
    )
    assert decision.reason is SuppressionReason.DUPLICATE


def test_key_after_the_window_delivers_again() -> None:
    category = _category(window=600)
    decision = decide(
        category, overrides={}, last_same_key_at=NOW - timedelta(seconds=600), now=NOW
    )
    assert decision.deliver


def test_zero_window_never_deduplicates() -> None:
    decision = decide(_category(window=0), overrides={}, last_same_key_at=NOW, now=NOW)
    assert decision.deliver
