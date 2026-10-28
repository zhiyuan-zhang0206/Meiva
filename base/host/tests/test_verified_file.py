# meiva:note
# The bounded read of one stable regular file over real files under `tmp_path`: what it returns, and
# that it refuses a symlink, a directory, a file over the budget and a budget that is not positive.
# `test_verified_file_plan.py` asserts what the observed stats refuse.

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.verified_file.reader import RegularFileReadError, regular_bytes


def test_a_regular_file_is_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_bytes(b"value\n")

    assert regular_bytes(path) == b"value\n"


def test_an_empty_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")

    assert regular_bytes(path) == b""


def test_a_file_at_the_budget_is_read_and_one_over_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "data"
    path.write_bytes(b"x" * 8)

    assert regular_bytes(path, max_bytes=8) == b"x" * 8
    with pytest.raises(RegularFileReadError, match="8 bytes, over its 7-byte read limit"):
        regular_bytes(path, max_bytes=7)


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(RegularFileReadError, match="is not a regular file"):
        regular_bytes(link)


def test_a_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(RegularFileReadError, match="is not a regular file"):
        regular_bytes(tmp_path)


def test_a_missing_file_is_the_filesystems_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        regular_bytes(tmp_path / "absent")


def test_a_budget_that_is_not_positive_is_refused_before_the_file_is_touched(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="budget must be positive"):
        regular_bytes(tmp_path / "absent", max_bytes=0)
