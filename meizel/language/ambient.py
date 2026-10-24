# meiva:note
# Where a file reaches the ambient world.
#
# An acquisition is the point at which code takes something from the process or the machine it
# runs in instead of being handed it: the clock, the environment, the home directory, the working
# directory, the platform, the process ids, randomness, and the process state a repository keeps
# itself (its configuration, its database, event bus and service endpoints). Only the point of
# acquisition is recorded. What the code does with the object afterwards is not followed: the
# analysis has no types, so `settings.x.y` after a `from ... import settings` is a plain attribute
# read of a name that was already acquired.
#
# The standard-library names come from the shared graph model. The names of the state a repository keeps
# are the repository's own: a file declares under `[state]` in its `meizel.toml` that it holds state of a
# kind, and the policy lists, per kind, the names through which that state is reached.

from __future__ import annotations

import ast
from collections.abc import Iterator

from meizel.graph.model import INTERVAL_CLOCKS, Acquisition, AmbientKind, Sources, stdlib_kind
from meizel.graph.universe import Universe
from meizel.language.patching import PatchForms
from meizel.lint_common import is_test_path

__all__ = ["AmbientAccess", "AmbientRecorder"]


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))


def _sleeps(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in _own_nodes(function):
        if isinstance(node, ast.Call):
            callee = node.func
            if (isinstance(callee, ast.Name) and callee.id == "sleep") or (
                isinstance(callee, ast.Attribute) and callee.attr == "sleep"
            ):
                return True
    return False


# meiva:note
# Collects the acquisitions of one file as the scanner visits it.
class AmbientRecorder:
    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self.items: list[Acquisition] = []
        self._waiting: list[bool] = []

    def enter(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._waiting.append(_sleeps(function))

    def leave(self) -> None:
        self._waiting.pop()

    def _add(self, line: int, kind: AmbientKind, target: str) -> None:
        self.items.append(Acquisition(line, kind, target))

    # meiva:note
    # A name imported with `from ... import`: state acquired where it is brought in.
    def imported(self, origin: str, line: int) -> None:
        kind = self.sources.names.get(origin)
        if kind is not None:
            self._add(line, kind, origin)

    # meiva:note
    # A name or attribute chain read in code. `bound` is the origin of its first name.
    def used(self, dotted: str, bound: str, line: int) -> None:
        found = stdlib_kind(dotted)
        if found is not None:
            kind, target = found
            if target not in INTERVAL_CLOCKS or (self._waiting and self._waiting[-1]):
                self._add(line, kind, target)
            return
        parts = dotted.split(".")
        for end in range(len(bound.split(".")) + 1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            kind = self.sources.names.get(prefix)
            if kind is not None:
                self._add(line, kind, prefix)
                return

    def called(self, callee: str | None, line: int) -> None:
        kind = None if callee is None else self.sources.calls.get(callee)
        if kind is not None and callee is not None:
            self._add(line, kind, callee)


# meiva:note
# A scanner that records where its file reaches the ambient world; test files record none.
class AmbientAccess(PatchForms):
    def __init__(
        self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool, sources: Sources
    ) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.ambient = AmbientRecorder(sources)
        self.records_ambient = not (self.test_file or is_test_path(path))
