# meiva:note
# Reading the new web login password from what the process entry hands in: the first line of a
# stream, or two entries at a prompt. The functions judge what came back and read nothing.

from __future__ import annotations

import io

import pytest

from cli.startup.password_input import (
    PasswordInputError,
    read_password_from_prompt,
    read_password_from_stdin,
)


@pytest.mark.parametrize(
    ("text", "password"),
    [
        ("secret-phrase-123\n", "secret-phrase-123"),
        ("secret-phrase-123\r\n", "secret-phrase-123"),
        ("secret-phrase-123", "secret-phrase-123"),
        ("  spaces  count  \n", "  spaces  count  "),
        ("first line\nsecond line\n", "first line"),
    ],
)
def test_stdin_gives_its_first_line_without_the_line_ending(text: str, password: str) -> None:
    assert read_password_from_stdin(io.StringIO(text)) == password


@pytest.mark.parametrize("text", ["", "\n", "\r\nsecond line\n"])
def test_an_empty_stdin_has_no_password(text: str) -> None:
    with pytest.raises(PasswordInputError, match="no password"):
        read_password_from_stdin(io.StringIO(text))


def test_the_prompt_asks_twice_and_returns_the_agreed_password() -> None:
    answers = iter(["typed password!", "typed password!"])
    asked: list[str] = []

    def ask(question: str) -> str:
        asked.append(question)
        return next(answers)

    assert read_password_from_prompt(ask) == "typed password!"
    assert len(asked) == 2 and asked[0] != asked[1]


def test_two_different_entries_are_not_a_password() -> None:
    answers = iter(["typed password!", "typed password?"])
    with pytest.raises(PasswordInputError, match="differ"):
        read_password_from_prompt(lambda _question: next(answers))
