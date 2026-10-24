# meiva:note
# The part of a split URL the decisions in this package look at.
#
# `urllib.parse.urlsplit` returns something that fits this shape, so a shell hands the parsed
# URL to a decision without that decision importing a parser.

from __future__ import annotations

from typing import Protocol

__all__ = ["UrlView"]


class UrlView(Protocol):
    @property
    def scheme(self) -> str: ...

    @property
    def netloc(self) -> str: ...

    @property
    def hostname(self) -> str | None: ...

    @property
    def port(self) -> int | None: ...

    @property
    def username(self) -> str | None: ...

    @property
    def password(self) -> str | None: ...

    @property
    def path(self) -> str: ...

    @property
    def query(self) -> str: ...

    @property
    def fragment(self) -> str: ...
