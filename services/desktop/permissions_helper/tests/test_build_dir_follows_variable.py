from __future__ import annotations

from pathlib import Path

from services.desktop.permissions_helper import lifecycle


def test_the_default_build_directory_follows_the_variable_after_import(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert lifecycle.build_directory(None, home=first) == first / "helper"
    assert lifecycle.build_directory(None, home=second) == second / "helper"
