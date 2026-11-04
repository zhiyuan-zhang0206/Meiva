# meiva:note
# The shape of a judgment of an ambient name, and the sentences the groups of the judged table share.
# The table itself is `ambient_surface.py` (`ambient_process_table.py` holds the process channels).

from __future__ import annotations

from dataclasses import dataclass

from meizel.graph.model import AmbientKind, Unless

__all__ = [
    "BUILD",
    "CONSTANT",
    "FILES",
    "NET",
    "STATE",
    "SYNTAX",
    "TYPE",
    "VALUES",
    "Group",
    "Judgment",
    "K",
    "g",
    "later",
]


# meiva:note
# What reading a name is, and why: no `kinds` is "not an ambient read". A call that reads several
# channels (`email.utils.make_msgid` the clock, the process id, entropy and the host name) lists
# every one of them, and each is acquired at the call. An `unless` clause spares the call as a
# whole, so it is written only where the parameter spares every kind the entry lists. `interval`
# marks a name that reports a duration rather than an instant: its judgment is the duration kind and
# nothing else, because how far apart two readings are is the one fact the flag is about.
@dataclass(frozen=True, slots=True)
class Judgment:
    kinds: tuple[AmbientKind, ...]
    why: str
    unless: Unless | None = None
    interval: bool = False

    def __post_init__(self) -> None:
        if len(set(self.kinds)) != len(self.kinds):
            raise ValueError(f"a kind is listed twice in {self.kinds!r}")
        if self.interval and self.kinds != (AmbientKind.MONOTONIC,):
            raise ValueError("an interval clock reads the duration and nothing else")
        if self.unless is not None and not self.kinds:
            raise ValueError("an `unless` clause spares a read, and this name reads nothing")


# meiva:note
# Names of one module (or class) that share one judgment; a name may be a `*` pattern.
@dataclass(frozen=True, slots=True)
class Group:
    module: str
    names: tuple[str, ...]
    judgment: Judgment


K = AmbientKind


# meiva:note
# A group of names with one judgment. `kind` is the one kind the names read, `None` when they read
# nothing, or a tuple of two or more kinds when one call reads several channels.
def g(
    module: str,
    names: str,
    kind: AmbientKind | tuple[AmbientKind, ...] | None,
    why: str,
    *,
    unless: Unless | None = None,
    interval: bool = False,
) -> Group:
    if isinstance(kind, tuple) and len(kind) < 2:
        raise ValueError("a judgment of one kind is spelled as the kind, not as a tuple")
    kinds = () if kind is None else kind if isinstance(kind, tuple) else (kind,)
    return Group(module, tuple(names.split()), Judgment(kinds, why, unless, interval))


def later(position: int, parameter: str) -> Unless:
    return Unless(parameter, position)


FILES = (
    "an operation on files, descriptors or processes: the purity layer judges it (`impure-call`); "
    "it learns nothing about the situation that the caller did not name"
)
NET = "a network operation or a protocol constant: the purity layer judges it (`impure-call`)"
CONSTANT = (
    "a constant of the operating system's headers or of the interpreter build: it selects an option "
    "of an API and identifies nothing about the host"
)
TYPE = "a type: naming it reads nothing"
VALUES = "converts, compares or formats the values it is given and reads nothing else"
STATE = (
    "the interpreter's own mutable state or a hook into it: its use is a write far more often than "
    "a decision, and a write acquires nothing"
)
BUILD = (
    "a constant of the interpreter build: the deployment pins the interpreter, so it does not vary "
    "between launches of one deployment"
)
SYNTAX = (
    "a path-syntax constant of the platform: it formats a path and does not identify the host "
    "(`os.name` and `sys.platform` do, and are listed)"
)
