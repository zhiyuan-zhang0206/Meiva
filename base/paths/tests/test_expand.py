# meiva:note
# `expand_home`: a leading tilde is the home the caller read, and nothing else is guessed.

from __future__ import annotations

from pathlib import Path

import pytest

from base.paths.expand import expand_home

_HOME = Path("/home/the-agent")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("~", _HOME),
        ("~/", _HOME),
        ("~/out/result.png", _HOME / "out/result.png"),
        (Path("~/out/result.png"), _HOME / "out/result.png"),
        ("~//out/result.png", _HOME / "out/result.png"),
        ("~///etc/passwd", _HOME / "etc/passwd"),
        ("~/./out/result.png", _HOME / "out/result.png"),
        ("result.png", Path("result.png")),
        ("/abs/result.png", Path("/abs/result.png")),
    ],
)
def test_a_leading_tilde_expands_against_the_home_the_caller_read(
    path: str | Path, expected: Path
) -> None:
    assert expand_home(path, user_home=_HOME) == expected


def test_a_path_without_a_tilde_is_kept_as_it_is() -> None:
    assert expand_home("plugins/x", user_home=_HOME) == Path("plugins/x")
    assert expand_home("", user_home=_HOME) == Path()


def test_another_users_home_is_refused_rather_than_resolved() -> None:
    with pytest.raises(ValueError, match="names another user's home"):
        expand_home("~someone/result.png", user_home=_HOME)
