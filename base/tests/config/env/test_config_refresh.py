# meiva:note
# refresh_data_plane_settings rebuilds the in-memory data-plane config in
# place after the unit's `.env` was rewritten under the process.
#
# The 2026-08-25 rollout incident: the local leg's `meiva start` ran a
# data-plane credential rotation (secret split) that rewrote `$MEIVA_HOME/.env`,
# while the orchestrating rollout process kept its startup-built Settings
# singleton — every later data-plane write then failed authentication. These
# tests pin the refresh contract with the one credential `.env` still carries,
# the Redis runtime credential. Refresh re-reads the new `.env`, rebuilds only
# `settings.data_plane` in place and leaves every other domain untouched.

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from base import config
from base.paths.tests.homes import point_home

_IDENTITY_LINES = [
    f"MEIVA_DB_URL={os.environ['MEIVA_DB_URL']}",
    f"MEIVA_REDIS_URL={os.environ['MEIVA_REDIS_URL']}",
    f"MEIVA_CLUSTER_SECRET={os.environ['MEIVA_CLUSTER_SECRET']}",
    f"MEIVA_GATEWAY_URL={os.environ['MEIVA_GATEWAY_URL']}",
]


@pytest.fixture(autouse=True)
def _restore_env_and_data_plane() -> Iterator[None]:
    """Restore os.environ and refresh the process-global data-plane sub-model
    after every refresh test."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        config.refresh_data_plane_settings()


def _point_env_at(monkeypatch: pytest.MonkeyPatch, env_text: str, tmp_path: Path) -> None:
    merged = tmp_path / ".env"
    # meiva:note
    # The unit's own lines come last: a later duplicate key wins.
    merged.write_text("\n".join(_IDENTITY_LINES) + "\n" + env_text)
    point_home(monkeypatch, tmp_path)


def _env_text(runtime_password: str) -> str:
    return (
        "MEIVA_CLUSTER_SECRET=sekret\n"
        f"MEIVA_REDIS_URL=redis://meiva:{runtime_password}@127.0.0.1:6380/0\n"
    )


def test_refresh_picks_up_rotated_redis_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # meiva:note
    # A Redis credential rotation on disk (scripts/data_plane_ops/rotate_data_plane_secrets.py)
    # is visible to the process after refresh: the in-memory redis_url carries the
    # new runtime password, and the old one is gone.
    _point_env_at(monkeypatch, _env_text("first-pass"), tmp_path)
    config.refresh_data_plane_settings()
    from base.config import settings

    assert "first-pass" in settings.data_plane.redis_url

    # meiva:note
    # Rotate on disk, refresh again — the same process now dials the new password.
    _point_env_at(monkeypatch, _env_text("rotated-pass"), tmp_path)
    config.refresh_data_plane_settings()
    assert "rotated-pass" in settings.data_plane.redis_url
    assert "first-pass" not in settings.data_plane.redis_url


def test_refresh_is_in_place_and_leaves_other_domains_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # meiva:note
    # Existing `from base.config import settings` references see the new
    # sub-model (rebuild is in place on the singleton), and non-data-plane
    # domains are not rebuilt (a rotation is a data-plane fact).
    from base.config import settings

    old_lm = settings.lm
    _point_env_at(monkeypatch, _env_text("first-pass"), tmp_path)
    config.refresh_data_plane_settings()
    # meiva:note
    # other domains untouched
    assert settings.lm is old_lm
    # meiva:note
    # but data plane refreshed
    assert "first-pass" in settings.data_plane.redis_url
