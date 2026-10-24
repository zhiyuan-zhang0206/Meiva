# meiva:note
# The parts of a split URL the decisions in this package look at, as one frozen value.
#
# A shell splits the raw string (`url_split.split_url`) and hands the result to a decision, so no
# decision imports a parser. The authority, the user name and the password can carry a login: none
# of them reaches a repr.

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["UrlParts"]


@dataclass(frozen=True)
class UrlParts:
    scheme: str
    netloc: str = field(repr=False)
    hostname: str | None
    port: int | None
    username: str | None = field(repr=False)
    password: str | None = field(repr=False)
    path: str
    query: str
    fragment: str
