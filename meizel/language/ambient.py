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

from meizel.graph.imports import Scope
from meizel.graph.model import (
    INTERVAL_CLOCKS,
    SEEDABLE_GENERATORS,
    Acquisition,
    AmbientKind,
    EntropySite,
    Sources,
    stdlib_kind,
)
from meizel.graph.universe import Universe
from meizel.language import fake_entropy
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
# Only a non-None literal proves an independent seed. A dynamic value can be None;
# unbounded positional or keyword arguments cannot prove that the seed was supplied.
def _ambient_seed(call: ast.Call) -> bool:
    if len(call.args) == 1 and not call.keywords:
        seed = call.args[0]
    elif not call.args and len(call.keywords) == 1 and call.keywords[0].arg == "x":
        seed = call.keywords[0].value
    else:
        return True
    try:
        return ast.literal_eval(seed) is None
    except (ValueError, TypeError):
        return True


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

    def called(self, callee: str | None, call: ast.Call) -> None:
        if callee is not None and callee in SEEDABLE_GENERATORS and _ambient_seed(call):
            self._add(call.lineno, AmbientKind.RANDOM, callee)
        kind = None if callee is None else self.sources.calls.get(callee)
        if kind is not None and callee is not None:
            self._add(call.lineno, kind, callee)


def _name_of(target: ast.expr) -> str:
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


# meiva:note
# A scanner that records where its file reaches the ambient world, and where it spends a reading
# that is not entropy as an identifier; test files record neither.
class AmbientAccess(PatchForms):
    def __init__(
        self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool, sources: Sources
    ) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.ambient = AmbientRecorder(sources)
        self.records_ambient = not (self.test_file or is_test_path(path))
        self.entropy_scopes: list[tuple[list[ast.stmt], Scope]] = []
        self.entropy_marked: list[bool] = [False]
        self.entropy_wrappers: list[tuple[str, int]] = []

    def entropy_enter(self) -> None:
        self.entropy_marked.append(False)

    def entropy_touch(self, callee: str | None, call: ast.Call) -> None:
        func = call.func
        if (
            callee in fake_entropy.ENTROPY_CLOCKS
            or fake_entropy.is_loop_clock(call, self.dotted)
            or (callee is None and isinstance(func, ast.Name) and func.id in ("id", "next"))
        ):
            self.entropy_marked[-1] = True

    def entropy_leave(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        inner: Scope,
        *,
        top_level: bool,
        method: bool,
    ) -> None:
        marked = self.entropy_marked.pop()
        if not self.records_ambient:
            return
        if marked and function.name not in ("__repr__", "__str__"):
            self.entropy_scopes.append((function.body, inner))
        if top_level or method:
            outer, self.scope = self.scope, inner
            clock = fake_entropy.wrapper_clock(function, self.dotted, method=method)
            self.scope = outer
            if clock is not None:
                self.entropy_wrappers.append((function.name, function.lineno))

    def _counter_names(self) -> frozenset[str]:
        names: set[str] = set()
        for node in ast.walk(self.tree):
            if (
                isinstance(node, ast.Assign | ast.AnnAssign)
                and isinstance(node.value, ast.Call)
                and self.dotted(node.value.func) in fake_entropy.COUNTER_FACTORIES
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(_name_of(target) for target in targets)
        return frozenset(names)

    def _used_as_value(self) -> frozenset[str]:
        called = {id(call.func) for call in ast.walk(self.tree) if isinstance(call, ast.Call)}
        names: set[str] = set()
        for node in ast.walk(self.tree):
            if id(node) in called:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
        return frozenset(names)

    # meiva:note
    # The module-level names that stand for an interval clock without being a function:
    # `_now = time.monotonic`, `_now = lambda: time.monotonic()`, `_now = partial(time.monotonic)`.
    def _clock_values(self) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for node in self.tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                name = (
                    targets[0].id if len(targets) == 1 and isinstance(targets[0], ast.Name) else ""
                )
                if name.startswith("_") and fake_entropy.value_wrapper(node.value, self.dotted):
                    found.append((name, node.lineno))
        return found

    def entropy_finish(self) -> list[EntropySite]:
        if not self.records_ambient:
            return []
        if self.entropy_marked[0]:
            self.entropy_scopes.append((self.tree.body, self.scope))
        counters = self._counter_names() if self.entropy_scopes else frozenset[str]()
        found: list[EntropySite] = []
        for body, scope in self.entropy_scopes:
            outer, self.scope = self.scope, scope
            found.extend(
                fake_entropy.function_sites(
                    body, self.dotted, lambda name: self.scope.find(name) == (None, False), counters
                )
            )
            self.scope = outer
        self.entropy_wrappers.extend(self._clock_values())
        if self.entropy_wrappers:
            used = self._used_as_value()
            found.extend(
                EntropySite(line, f"interval-wrapper {name}")
                for name, line in self.entropy_wrappers
                if name not in used
            )
        return found
