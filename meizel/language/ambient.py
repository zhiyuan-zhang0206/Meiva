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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace

from meizel.graph.imports import Scope
from meizel.graph.model import (
    INTERVAL_CLOCKS,
    LOOP_CLOCK,
    OPEN_CALLS,
    SEEDABLE_GENERATORS,
    Acquisition,
    AmbientKind,
    EntropySite,
    Sources,
    Unless,
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


# meiva:note
# What the recorder keeps of one function while the file is scanned: whether its own statements
# sleep, the module-level names it calls, whether it is a one-line wrapper over an interval clock,
# and the interval-clock readings it makes. Whether a reading counts is decided
# when the file is done, because a sleep may be a local helper defined further down.
@dataclass(frozen=True, slots=True)
class _Frame:
    name: str
    top_level: bool
    sleeps: bool
    calls: Mapping[str, int]
    wrapper: bool
    reads: tuple[Acquisition, ...] = ()
    caller: frozenset[str] = frozenset()


def _called_name(callee: ast.expr) -> str:
    if isinstance(callee, ast.Name):
        return callee.id
    return callee.attr if isinstance(callee, ast.Attribute) else ""


# meiva:note
# A call whose name says it waits (`sleep`, `asleep`, `retry_sleep`) is a sleep; so is a call of a
# function of this module that sleeps. The names a function calls are kept to follow the second.
# The same walk notes whether the function expands a path at all, which is the only reason to ask
# which of its names carry the caller's values.
def _own_facts(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, dict[str, int], bool]:
    sleeps = False
    expands = False
    calls: dict[str, int] = {}
    for node in _own_nodes(function):
        if isinstance(node, ast.Call):
            sleeps = sleeps or "sleep" in _called_name(node.func).lower()
            expands = expands or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "expanduser"
            )
            if isinstance(node.func, ast.Name):
                calls.setdefault(node.func.id, node.lineno)
    return sleeps, calls, expands


# meiva:note
# How many arguments a call gives; an unpacked argument or keyword set counts as more than any limit.
def _given(call: ast.Call) -> int:
    unpacked = any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    )
    return 99 if unpacked else len(call.args) + len(call.keywords)


# meiva:note
# Whether the call hands the reader the value that spares its read. An explicit `None` is still a
# read, and an argument list whose shape is hidden (`*args`) cannot prove that a value was given.
def _spares(call: ast.Call, unless: Unless | None) -> bool:
    if unless is None:
        return False
    for keyword in call.keywords:
        if keyword.arg == unless.parameter:
            return not _is_none(keyword.value)
    position = unless.position
    if position is None or len(call.args) <= position:
        return False
    if any(isinstance(argument, ast.Starred) for argument in call.args[: position + 1]):
        return False
    return not _is_none(call.args[position])


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


# meiva:note
# The dotted module a file defines (`pkg/mod.py` -> `pkg.mod`, `pkg/__init__.py` -> `pkg`), needed
# here to spell a call of a function this same file defines.
def _module_of(path: str) -> str:
    parts = path.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


# meiva:note
# The kind of read a call is: None when it names no reader, and None when the call hands the reader
# the value that spares the read, so that `called` stays a list of the acquisitions it adds.
def _reader_of(
    call: ast.Call, callee: str | None, sources: Sources
) -> tuple[AmbientKind, str] | None:
    if callee is None:
        return None
    kind = sources.calls.get(callee)
    if kind is None or _spares(call, sources.unless.get(callee)):
        return None
    return kind, callee


# meiva:note
# `expanduser()` on a path (`Path("~").expanduser()`) reads the user's home; `os.path.expanduser`
# is judged by name where it is read.
def _expands_home(call: ast.Call, callee: str | None) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr == "expanduser" and callee is None


# meiva:note
# The path an `expanduser()` call expands: the receiver of the method call.
def _expanded_receiver(call: ast.Call) -> ast.expr | None:
    func = call.func
    return func.value if isinstance(func, ast.Attribute) else None


# meiva:note
# The names one function holds from its caller: its parameters, and the locals one derivation away
# from them (`p = Path(path)` from parameter `path`, a loop variable over parameter `parts`). A
# name a statement binds from anything else stops counting, and so does a name bound twice — once
# from a parameter and once from elsewhere. One hop, not a chain: a longer derivation is recorded,
# which keeps the analysis a single pass and its misses on the side of the world.
def _caller_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    args = function.args
    owned = {argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            owned.add(extra.arg)
    # meiva:note
    # The receiver of a method is not followed: attributes are not tracked, so what a constructor
    # stored on `self` records where it was read, not at the call that expands it.
    owned -= _RECEIVERS
    derived: set[str] = set()
    lost: set[str] = set()
    for node in _own_nodes(function):
        for bound, source in _bindings(node):
            if source is not None and _mentions(source, owned):
                derived.add(bound)
            else:
                lost.add(bound)
    return frozenset((owned | derived) - lost)


# meiva:note
# The names every method receives as its receiver; the analysis follows no attributes.
_RECEIVERS = frozenset({"self", "cls"})


# meiva:note
# What one statement binds and the expression it binds it from (`None`: it rebinds from
# something the analysis does not follow, so the name stops reading as the caller's).
def _bindings(node: ast.AST) -> Iterator[tuple[str, ast.expr | None]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            yield from ((name, node.value) for name in _bound_names(target))
    elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
        yield from ((name, node.value) for name in _bound_names(node.target))
    elif isinstance(node, ast.AugAssign):
        yield from ((name, None) for name in _bound_names(node.target))
    elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
        yield from ((name, node.iter) for name in _bound_names(node.target))


def _bound_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        names: set[str] = set()
        for element in target.elts:
            names |= _bound_names(element)
        return names
    return set()


def _mentions(expr: ast.expr, names: set[str] | frozenset[str]) -> bool:
    return any(isinstance(node, ast.Name) and node.id in names for node in ast.walk(expr))


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
#
# An interval clock is a window onto the world only in a function that waits, because then how long
# to wait next is decided in a loop; so its readings are held by the function and counted when the
# file is done and the functions that wait are known: one that calls `sleep` or a name that contains
# it, and one that calls a function of the module that does. A module-level function that is only
# `return time.monotonic()` is a clock, and a call of it in a function that waits is a reading.
class AmbientRecorder:
    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self.items: list[Acquisition] = []
        self._open: list[_Frame] = []
        self._reads: list[list[Acquisition]] = []
        self._done: list[_Frame] = []

    def enter(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, *, top_level: bool = False
    ) -> None:
        sleeps, calls, expands = _own_facts(function)
        self._open.append(
            _Frame(
                function.name,
                top_level,
                sleeps,
                calls,
                wrapper=False,
                caller=_caller_names(function) if expands else frozenset(),
            )
        )
        self._reads.append([])

    def leave(self, *, wrapper: bool = False) -> None:
        frame = replace(self._open.pop(), wrapper=wrapper, reads=tuple(self._reads.pop()))
        self._done.append(frame)

    def _add(self, line: int, kind: AmbientKind, target: str) -> None:
        self.items.append(Acquisition(line, kind, target))

    def _interval(self, line: int, target: str) -> None:
        if self._reads:
            self._reads[-1].append(Acquisition(line, AmbientKind.CLOCK, target))

    # meiva:note
    # Whether `expr` is built from values of the function's own caller (its parameters and what is
    # derived from them). Expanding a `~` in one of those is the read the caller already named by
    # writing it, so it is allowed and records nothing.
    def _from_caller(self, expr: ast.expr | None) -> bool:
        return expr is not None and bool(self._open) and _mentions(expr, self._open[-1].caller)

    # meiva:note
    # A name imported with `from ... import`: state acquired where it is brought in.
    def imported(self, origin: str, line: int) -> None:
        kind = self.sources.names.get(origin)
        if kind is not None and origin not in self.sources.uses:
            self._add(line, kind, origin)

    # meiva:note
    # A name or attribute chain read in code. `bound` is the origin of its first name.
    def used(self, dotted: str, bound: str, line: int) -> None:
        if bound in self.sources.uses:
            self._add(line, self.sources.names[bound], bound)
            return
        found = stdlib_kind(dotted)
        if found is not None:
            kind, target = found
            if target in INTERVAL_CLOCKS:
                self._interval(line, target)
            else:
                self._add(line, kind, target)
            return
        parts = dotted.split(".")
        for end in range(len(bound.split(".")) + 1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            kind = self.sources.names.get(prefix)
            if kind is not None:
                self._add(line, kind, prefix)
                return

    def called(self, callee: str | None, call: ast.Call, *, loop_clock: bool = False) -> None:
        if callee is not None and callee in SEEDABLE_GENERATORS and _ambient_seed(call):
            self._add(call.lineno, AmbientKind.RANDOM, callee)
        if loop_clock:
            self._interval(call.lineno, LOOP_CLOCK)
        open_call = None if callee is None else OPEN_CALLS.get(callee)
        if open_call is not None and callee is not None and _given(call) <= open_call[1]:
            self._add(call.lineno, open_call[0], f"{callee}()")
        if _expands_home(call, callee) and not self._from_caller(_expanded_receiver(call)):
            self._add(call.lineno, AmbientKind.HOME, "pathlib.Path.expanduser")
        reader = _reader_of(call, callee, self.sources)
        if reader is not None:
            self._add(call.lineno, *reader)

    # meiva:note
    # The acquisitions of the file once every function is known. The functions of the module that
    # wait are closed under calls: `_pause()` that sleeps makes its callers wait.
    def finish(self) -> list[Acquisition]:
        local = {frame.name: frame for frame in self._done if frame.top_level}
        waiting = _closure_of_waiters(local)
        clocks = {name for name, frame in local.items() if frame.wrapper and len(frame.reads) == 1}
        for frame in self._done:
            if frame.sleeps or waiting.intersection(frame.calls):
                self.items.extend(frame.reads)
                self.items.extend(
                    Acquisition(line, AmbientKind.CLOCK, f"{name}()")
                    for name, line in frame.calls.items()
                    if name in clocks
                )
        self.items.sort(key=lambda found: (found.line, found.kind.value, found.target))
        return self.items


# meiva:note
# The module-level functions that wait: those that sleep, and those that call one that waits.
def _closure_of_waiters(local: Mapping[str, _Frame]) -> set[str]:
    waiting = {name for name, frame in local.items() if frame.sleeps}
    grew = True
    while grew:
        grew = False
        for name, frame in local.items():
            if name not in waiting and waiting.intersection(frame.calls):
                waiting.add(name)
                grew = True
    return waiting


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
        self.sources = sources
        self.records_ambient = not (self.test_file or is_test_path(path))
        self.module = _module_of(path)
        self.module_defs = frozenset(
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        )
        self.entropy_scopes: list[tuple[list[ast.stmt], Scope]] = []
        self.entropy_marked: list[bool] = [False]
        self.entropy_wrappers: list[tuple[str, int]] = []

    def entropy_enter(self) -> None:
        self.entropy_marked.append(False)

    def record_call(self, callee: str | None, call: ast.Call) -> None:
        if callee is None:
            callee = self._local_callee(call.func)
        self.ambient.called(callee, call, loop_clock=fake_entropy.is_loop_clock(call, self.dotted))

    # meiva:note
    # The module-level function of this same file that a bare call names, when its entry says a
    # passed parameter spares the read. The import resolver knows only imported names, so without
    # this the clause could not hold where a wrapper sits beside its caller. It is asked only for
    # the readers that declare a clause: a reader this file defines is already recorded by this
    # file's own entry for the kind, so resolving every entry here would record one read twice
    # (measured 2026-10-30: six sites, each in a file that already carries its kind). The answer
    # feeds the ambient rule alone: facts, locality and purity keep their own resolution.
    def _local_callee(self, func: ast.expr) -> str | None:
        if not isinstance(func, ast.Name) or func.id not in self.module_defs:
            return None
        target = f"{self.module}.{func.id}"
        if target not in self.sources.unless:
            return None
        scope = self.scope
        while scope.parent is not None:
            if func.id in scope.shadowed or func.id in scope.imports:
                return None
            scope = scope.parent
        return target

    # meiva:note
    # The clock a function is a one-line wrapper over, judged with the names of its own scope. Only a
    # module-level function or a method can be called by name from the rest of the file.
    def clock_wrapper(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        inner: Scope,
        *,
        top_level: bool,
        method: bool,
    ) -> str | None:
        if not (self.records_ambient and (top_level or method)):
            return None
        outer, self.scope = self.scope, inner
        try:
            return fake_entropy.wrapper_clock(function, self.dotted, method=method)
        finally:
            self.scope = outer

    def entropy_touch(self, callee: str | None, call: ast.Call) -> None:
        func = call.func
        if (
            callee in INTERVAL_CLOCKS
            or fake_entropy.is_loop_clock(call, self.dotted)
            or (callee is None and isinstance(func, ast.Name) and func.id in ("id", "next"))
        ):
            self.entropy_marked[-1] = True

    def entropy_leave(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        inner: Scope,
        *,
        clock: str | None,
    ) -> None:
        marked = self.entropy_marked.pop()
        if not self.records_ambient:
            return
        if marked and function.name not in ("__repr__", "__str__"):
            self.entropy_scopes.append((function.body, inner))
        if clock is not None and function.name.startswith("_"):
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
