# meiva:note
# What a bounded read of one stable regular file refuses, asserted as values built from synthetic
# stats: the budget, the entry before it is opened, the opened file and the file after the read.
# `test_verified_file.py` reads real files.

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from base.host.verified_file.plan import (
    refusal_after_read,
    refusal_before_open,
    refusal_on_open,
    require_budget,
)

_PATH = Path("/units/main/.env")
_LIMIT = 1024


def _stat(
    mode: int = stat.S_IFREG | 0o600,
    *,
    size: int = 10,
    dev: int = 7,
    ino: int = 99,
    mtime_ns: int = 5_000_000_000,
) -> os.stat_result:
    seconds = mtime_ns // 1_000_000_000
    return os.stat_result(
        (mode, ino, dev, 1, 0, 0, size, seconds, seconds, seconds, 0.0, 0.0, 0.0, 0, mtime_ns, 0)
    )


@pytest.mark.parametrize("budget", [0, -1, -1024])
def test_a_budget_that_is_not_positive_is_refused(budget: int) -> None:
    with pytest.raises(ValueError, match="budget must be positive"):
        require_budget(budget)


def test_a_positive_budget_is_accepted() -> None:
    require_budget(1)


@pytest.mark.parametrize(
    "mode", [stat.S_IFDIR | 0o755, stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFSOCK]
)
def test_an_entry_that_is_not_a_regular_file_is_refused_before_it_is_opened(mode: int) -> None:
    assert refusal_before_open(_PATH, _stat(mode), _LIMIT) == f"{_PATH} is not a regular file"


def test_a_file_over_the_budget_is_refused_naming_both_sizes() -> None:
    assert refusal_before_open(_PATH, _stat(size=_LIMIT + 1), _LIMIT) == (
        f"{_PATH} is {_LIMIT + 1} bytes, over its {_LIMIT}-byte read limit"
    )


def test_a_file_at_the_budget_is_not_refused() -> None:
    assert refusal_before_open(_PATH, _stat(size=_LIMIT), _LIMIT) is None


def test_the_file_that_was_checked_is_the_file_that_was_opened() -> None:
    assert refusal_on_open(_PATH, _stat(), _stat()) is None


@pytest.mark.parametrize(
    "opened",
    [
        _stat(stat.S_IFDIR | 0o755),
        _stat(dev=8),
        _stat(ino=100),
    ],
)
def test_another_file_than_the_one_checked_is_refused_on_open(opened: os.stat_result) -> None:
    assert refusal_on_open(_PATH, _stat(), opened) == f"{_PATH} changed while opening"


def _after_read(
    *,
    body_length: int = 10,
    opened: os.stat_result | None = None,
    after: os.stat_result | None = None,
    current: os.stat_result | None = None,
) -> str | None:
    return refusal_after_read(
        _PATH,
        body_length=body_length,
        max_bytes=_LIMIT,
        opened=opened or _stat(),
        after=after or _stat(),
        current=current or _stat(),
    )


def test_a_read_that_saw_nothing_move_is_trusted() -> None:
    assert _after_read() is None
    assert _after_read(body_length=_LIMIT) is None


@pytest.mark.parametrize(
    "refusal",
    [
        _after_read(body_length=_LIMIT + 1),
        _after_read(after=_stat(size=11)),
        _after_read(after=_stat(mtime_ns=6_000_000_000)),
        _after_read(current=_stat(ino=100)),
        _after_read(current=_stat(dev=8)),
    ],
)
def test_a_read_over_the_budget_or_of_a_file_that_moved_is_refused(refusal: str | None) -> None:
    assert refusal == f"{_PATH} changed while reading"
