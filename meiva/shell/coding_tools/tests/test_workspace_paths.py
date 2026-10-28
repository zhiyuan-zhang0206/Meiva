# meiva:note
# The workspace and companion-file paths a launcher takes from its command line: the leading `~` is
# expanded against the home the entry read, a `~someone` spelling is refused instead of read from the
# user database, and a relative companion file is taken against the workspace.
from __future__ import annotations

from pathlib import Path

import pytest

from meiva.shell.coding_tools._common import placed_path, resolve_dir, resolve_file


def test_a_tilde_workspace_expands_against_the_home_the_entry_read(tmp_path: Path) -> None:
    home = tmp_path / "operator"
    workspace = home / "ws"
    workspace.mkdir(parents=True)

    assert resolve_dir("~/ws", user_home=home) == workspace


def test_a_missing_workspace_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        resolve_dir("~/absent", user_home=tmp_path)

    assert exit_info.value.code == 1
    assert "is not a directory or does not exist" in capsys.readouterr().err


def test_another_accounts_home_is_refused_rather_than_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # meiva:note
    # `~someone/ws` names a home this process never read. Expanding it would answer from the user
    # database; the entry refuses it with one line and exit 1 instead.
    with pytest.raises(SystemExit) as exit_info:
        resolve_dir("~someone/ws", user_home=tmp_path)

    assert exit_info.value.code == 1
    assert "names another user's home" in capsys.readouterr().err


def test_a_companion_file_is_placed_like_the_workspace(tmp_path: Path) -> None:
    home = tmp_path / "operator"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert resolve_file(workspace, "~/tasks.md", user_home=home) == home / "tasks.md"
    assert resolve_file(workspace, "work.md", user_home=home) == workspace / "work.md"
    assert resolve_file(workspace, str(tmp_path / "abs.md"), user_home=home) == tmp_path / "abs.md"


def test_a_placed_path_leaves_a_relative_one_for_the_caller(tmp_path: Path) -> None:
    assert placed_path("ws", user_home=tmp_path) == Path("ws")
    assert placed_path("~", user_home=tmp_path) == tmp_path
    assert placed_path("~//ws", user_home=tmp_path) == tmp_path / "ws"
