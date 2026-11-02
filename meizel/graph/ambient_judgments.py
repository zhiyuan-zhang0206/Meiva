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
# What reading a name is, and why: `kind` None is "not an ambient read".
@dataclass(frozen=True, slots=True)
class Judgment:
    kind: AmbientKind | None
    why: str
    unless: Unless | None = None
    interval: bool = False


# meiva:note
# Names of one module (or class) that share one judgment; a name may be a `*` pattern.
@dataclass(frozen=True, slots=True)
class Group:
    module: str
    names: tuple[str, ...]
    judgment: Judgment


K = AmbientKind


def g(
    module: str,
    names: str,
    kind: AmbientKind | None,
    why: str,
    *,
    unless: Unless | None = None,
    interval: bool = False,
) -> Group:
    return Group(module, tuple(names.split()), Judgment(kind, why, unless, interval))


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
