from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_usage.types import (
    ERROR_STALE_AFTER_S,
    AntigravityQuotaGroups,
    RateWindow,
    WindowPair,
    resolve_user_path,
    should_write_failure,
)


def test_should_write_failure() -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    assert should_write_failure(None, None, now) is True
    assert should_write_failure("ok", None, now) is True
    assert should_write_failure(None, now, now) is True

    recent_updated = now - timedelta(seconds=60)
    assert should_write_failure("error", recent_updated, now) is True

    assert should_write_failure("ok", recent_updated, now) is False
    assert should_write_failure("warn", recent_updated, now) is False

    stale_updated = now - timedelta(seconds=ERROR_STALE_AFTER_S + 1)
    assert should_write_failure("ok", stale_updated, now) is True
    assert should_write_failure("warn", stale_updated, now) is True


def test_resolve_user_path(tmp_path: Path) -> None:
    home = tmp_path / "fakehome"
    home.mkdir()

    assert resolve_user_path("~", user_home=home) == home

    assert resolve_user_path("~/my/creds.json", user_home=home) == home / "my" / "creds.json"

    abs_path = "/var/secrets/key.json"
    assert resolve_user_path(abs_path, user_home=home) == Path(abs_path)

    with pytest.raises(ValueError, match="must not be empty"):
        resolve_user_path("", user_home=home)

    with pytest.raises(ValueError, match="must not be empty"):
        resolve_user_path("   ", user_home=home)


def test_window_pair_and_quota_groups() -> None:
    empty = WindowPair()
    assert empty.has_any() is False
    assert empty.session is None
    assert empty.weekly is None

    session_only = WindowPair(session=RateWindow(used_percent=10.0))
    assert session_only.has_any() is True

    weekly_only = WindowPair(weekly=RateWindow(used_percent=20.0))
    assert weekly_only.has_any() is True

    both = WindowPair(
        session=RateWindow(used_percent=10.0),
        weekly=RateWindow(used_percent=20.0),
    )
    assert both.has_any() is True

    groups = AntigravityQuotaGroups(gemini=both, claude_gpt=session_only)
    assert groups.gemini == both
    assert groups.claude_gpt == session_only
