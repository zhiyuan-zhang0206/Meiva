"""Unit tests for meiva_usage pure types and helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from meiva_builtins.plugins.meiva_usage.types import (
    ERROR_STALE_AFTER_S,
    resolve_user_path,
    should_write_failure,
)


def test_should_write_failure() -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

    # None status or None updated_at -> True
    assert should_write_failure(None, None, now) is True
    assert should_write_failure("ok", None, now) is True
    assert should_write_failure(None, now, now) is True

    # Error status -> always True
    recent_updated = now - timedelta(seconds=60)
    assert should_write_failure("error", recent_updated, now) is True

    # Fresh ok/warn status (within max_age_s) -> False (keep prior successful card)
    assert should_write_failure("ok", recent_updated, now) is False
    assert should_write_failure("warn", recent_updated, now) is False

    # Aged ok status (> max_age_s) -> True (record failure)
    stale_updated = now - timedelta(seconds=ERROR_STALE_AFTER_S + 1)
    assert should_write_failure("ok", stale_updated, now) is True
    assert should_write_failure("warn", stale_updated, now) is True


def test_resolve_user_path(tmp_path: Path) -> None:
    home = tmp_path / "fakehome"
    home.mkdir()

    # Tilde only
    assert resolve_user_path("~", user_home=home) == home

    # Tilde with subpath
    assert resolve_user_path("~/my/creds.json", user_home=home) == home / "my" / "creds.json"

    # Absolute path
    abs_path = "/var/secrets/key.json"
    assert resolve_user_path(abs_path, user_home=home) == Path(abs_path)

    # Empty raises ValueError
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_user_path("", user_home=home)

    with pytest.raises(ValueError, match="must not be empty"):
        resolve_user_path("   ", user_home=home)
