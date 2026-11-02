from __future__ import annotations

import stat
from pathlib import Path

import pytest

from base.host.db_authority.api import (
    ensure_telemetry_token,
    read_telemetry_token,
    telemetry_token_path,
)

_FIRST = "first-" + "f" * 40
_SECOND = "second-" + "s" * 40


def test_a_home_without_a_token_publishes_the_fresh_one_owner_only(tmp_path: Path) -> None:
    ensure_telemetry_token(tmp_path, _FIRST)

    assert read_telemetry_token(tmp_path) == _FIRST
    assert stat.S_IMODE(telemetry_token_path(tmp_path).stat().st_mode) == 0o600


def test_a_published_token_stays_whatever_fresh_value_a_later_start_draws(tmp_path: Path) -> None:
    ensure_telemetry_token(tmp_path, _FIRST)
    ensure_telemetry_token(tmp_path, _SECOND)

    assert read_telemetry_token(tmp_path) == _FIRST


# meiva:note
# The value a release that derived the token from the human secret left behind.
def test_a_token_published_by_an_earlier_release_is_kept_as_it_is(tmp_path: Path) -> None:
    derived = "d" * 64
    ensure_telemetry_token(tmp_path, derived)

    ensure_telemetry_token(tmp_path, _FIRST)

    assert read_telemetry_token(tmp_path) == derived


def test_a_blank_published_file_is_replaced_by_the_fresh_token(tmp_path: Path) -> None:
    path = telemetry_token_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_text("\n")

    ensure_telemetry_token(tmp_path, _FIRST)

    assert read_telemetry_token(tmp_path) == _FIRST


@pytest.mark.parametrize("fresh", ["", "  \n"])
def test_a_blank_fresh_token_is_refused_and_publishes_nothing(tmp_path: Path, fresh: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        ensure_telemetry_token(tmp_path, fresh)

    assert read_telemetry_token(tmp_path) is None
    assert not telemetry_token_path(tmp_path).exists()


def test_a_blank_fresh_token_leaves_the_published_one(tmp_path: Path) -> None:
    ensure_telemetry_token(tmp_path, _FIRST)
    with pytest.raises(ValueError, match="non-blank"):
        ensure_telemetry_token(tmp_path, "")

    assert read_telemetry_token(tmp_path) == _FIRST


def test_a_home_that_never_published_reads_none(tmp_path: Path) -> None:
    assert read_telemetry_token(tmp_path) is None
