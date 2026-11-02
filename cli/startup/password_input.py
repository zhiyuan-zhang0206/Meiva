# meiva:note
# Reading the new web login password from what the process entry hands in: a no-echo prompt or the
# first line of a stream. The entry owns the terminal (`cli.main`); these functions only judge what
# came back, so they read nothing themselves.

from __future__ import annotations

from collections.abc import Callable
from typing import TextIO

__all__ = [
    "PasswordInputError",
    "read_password_from_prompt",
    "read_password_from_stdin",
]


# meiva:note
# The password cannot be read as the operator asked: nothing was sent.
class PasswordInputError(ValueError):
    pass


# meiva:note
# The first line of `stream` without its line ending. Spaces are part of a password, so only the
# line ending goes.
def read_password_from_stdin(stream: TextIO) -> str:
    password = stream.readline().rstrip("\r\n")
    if not password:
        raise PasswordInputError("standard input held no password (its first line is empty)")
    return password


# meiva:note
# The password typed twice at `ask`, a no-echo prompt that takes its question and returns the answer.
def read_password_from_prompt(ask: Callable[[str], str]) -> str:
    password = ask("New web login password: ")
    if ask("Repeat the new password: ") != password:
        raise PasswordInputError("the two entries differ")
    return password
