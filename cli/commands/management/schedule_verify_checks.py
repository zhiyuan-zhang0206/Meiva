# meiva:note
# The three checks `meiva schedules verify` runs on one schedule script, decided over the parsed
# source and the names the script's own imports bound. Nothing here reads the filesystem or the
# process: `call_site_problems` takes the "is this callee repository code" predicate from its
# caller, and the child that does the reading (`schedule_verify_child.py`) supplies it -- the
# filesystem read is the child's, the rule that turns that read into an answer is `is_repo_source`
# here.
#
# 1. **Names** (`undefined_name_problems`) — every `Name` the script reads (any scope) must be one
#    it binds, a builtin, or an implicit module global. After the `shared` -> `base` rename,
#    `_ROOT = Path(base.__file__)` read a `base` nothing imported: the import check stayed green
#    and the first real fire died with `NameError` (2026-10-01 audit: three of 16 copies).
#    Conservative by construction: a name bound anywhere counts, annotation expressions are never
#    read, and a `from x import *` disables the check outright.
# 2. **Call sites** (`call_site_problems`) — every call whose callee resolves through those imports
#    to repo code must `inspect.signature(...).bind` the arguments the script passes. A signature
#    that moved on (2026-10-03: `catch_up()` and `fire_slot_once()` gained a required `db`) fails
#    here, where the import check stays green. Only the argument shape is bound; a call that cannot
#    be bound statically is skipped, never guessed, and the agent SDK is skipped entirely (plugins
#    install its namespaces and wrap its functions at load time).
from __future__ import annotations

import ast
import builtins
import inspect
import types
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

__all__ = ["call_site_problems", "is_repo_source", "undefined_name_problems"]


# meiva:note
# Names the script binds anywhere other than its top-level imports; a call through one of
# these is not (provably) a call of the imported object.
def _rebound_names(tree: ast.Module) -> set[str]:
    top_imports = {id(node) for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)}
    names: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in top_imports:
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            names.add(node.id)
        elif isinstance(
            node,
            ast.FunctionDef
            | ast.AsyncFunctionDef
            | ast.ClassDef
            | ast.TypeVar
            | ast.ParamSpec
            | ast.TypeVarTuple,
        ):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


# meiva:note
# `a.b.c` -> ("a", ["b", "c"]); anything not rooted in a plain name -> None.
def _callee_chain(func: ast.expr) -> tuple[str, list[str]] | None:
    attrs: list[str] = []
    while isinstance(func, ast.Attribute):
        attrs.append(func.attr)
        func = func.value
    if not isinstance(func, ast.Name):
        return None
    return func.id, attrs[::-1]


# meiva:note
# The `meiva` SDK package or a submodule: its surface is installed by plugins at load time.
def _is_sdk(target: object) -> bool:
    return isinstance(target, types.ModuleType) and target.__name__.split(".")[0] == "meiva"


# meiva:note
# The object a callee chain names, and (when a module lost the attribute) the missing name.
def _resolve(
    namespace: Mapping[str, object], chain: tuple[str, list[str]]
) -> tuple[Any, str | None]:
    target: Any = namespace[chain[0]]
    for attr in chain[1]:
        if _is_sdk(target):
            return None, None
        if isinstance(target, types.ModuleType) and not hasattr(target, attr):
            return None, f"{target.__name__} has no `{attr}`"
        try:
            target = getattr(target, attr)
        except AttributeError:
            return None, None
    return target, None


# meiva:note
# Calls rooted in a name the imports bound and the script never rebinds, with a fixed argument
# shape (no `*args` / `**kwargs` at the call site).
def _static_calls(
    tree: ast.Module, namespace: Mapping[str, object]
) -> Iterator[tuple[ast.Call, tuple[str, list[str]]]]:
    rebound = _rebound_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _callee_chain(node.func)
        if chain is None or chain[0] in rebound or chain[0] not in namespace:
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args):
            continue
        if any(kw.arg is None for kw in node.keywords):
            continue
        yield node, chain


def _problem(
    node: ast.Call,
    chain: tuple[str, list[str]],
    namespace: Mapping[str, object],
    is_repo: Callable[[object], bool],
) -> str | None:
    dotted = ".".join([chain[0], *chain[1]])
    target, missing = _resolve(namespace, chain)
    if missing is not None:
        return f"L{node.lineno} {dotted}(): {missing}"
    if target is None or not callable(target) or not is_repo(target):
        return None
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    positional = [object() for _ in node.args]
    keywords = {kw.arg: object() for kw in node.keywords if kw.arg}
    try:
        signature.bind(*positional, **keywords)
    except TypeError as exc:
        return f"L{node.lineno} {dotted}(): {exc} (is {dotted}{signature})"
    return None


# meiva:note
# One `L<line> <callee>(): <why>` per call whose arguments the callee cannot bind.
# meiva:note
# What counts as repository code for the call-site check: a callee defined in a file of this
# checkout, never one from a virtualenv or an installed package, and never one from the `meiva`
# SDK package (plugins install its namespaces and wrap its functions at load time, so the static
# module is neither the set of names nor the call contract). `source` is the file the process
# entry found for the callee and already resolved -- resolving a path is a filesystem read and
# stays in the entry; the classification it feeds is decided here. `None` (no file at all, or an
# object the filesystem cannot place) is never repository code.
def is_repo_source(source: Path | None, *, root: Path) -> bool:
    if source is None:
        return False
    if not source.is_relative_to(root) or {".venv", "site-packages"} & set(source.parts):
        return False
    return source.relative_to(root).parts[0] != "meiva"


def call_site_problems(
    tree: ast.Module, namespace: Mapping[str, object], *, is_repo: Callable[[object], bool]
) -> list[str]:
    found = (
        _problem(node, chain, namespace, is_repo) for node, chain in _static_calls(tree, namespace)
    )
    return [problem for problem in found if problem is not None]


# meiva:note
# What a script may read without binding it: the builtins and the module-internal implicit globals
# the runtime provides. Kept here, with the name check that consults them.
_BUILTIN_NAMES = frozenset(dir(builtins))
_IMPLICIT_GLOBALS = frozenset(
    {
        "__annotations__",
        "__builtins__",
        "__class__",
        "__debug__",
        "__dict__",
        "__doc__",
        "__file__",
        "__loader__",
        "__name__",
        "__package__",
        "__spec__",
    }
)


# meiva:note
# Every name the script binds: its rebinds (`_rebound_names`, which excludes the top-level
# imports), those imports themselves, the pattern-matching captures, and what the language
# provides.
def _bound_names(tree: ast.Module) -> set[str]:
    bound = _rebound_names(tree)
    for node in tree.body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound | _BUILTIN_NAMES | _IMPLICIT_GLOBALS


# meiva:note
# First line of every `Name` read, annotations excluded: the house style imports
# `from __future__ import annotations`, so annotation expressions are never evaluated.
class _NameReads(ast.NodeVisitor):
    def __init__(self) -> None:
        self.first_line: dict[str, int] = {}

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.first_line.setdefault(node.id, node.lineno)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # meiva:note
    # Decorators, bases, keywords and body read as usual; type-parameter bounds/defaults are
    # annotation-like and never read.
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in (*node.decorator_list, *node.bases, *node.keywords, *node.body):
            self.visit(child)

    # meiva:note
    # Only the value reads; the name binds and type-parameter bounds/defaults never read.
    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:
        self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_defaults(node.args)
        self.visit(node.body)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_defaults(node.args)
        for statement in node.body:
            self.visit(statement)

    # meiva:note
    # Only the default expressions: an arg node contributes nothing but an annotation.
    def _visit_defaults(self, arguments: ast.arguments) -> None:
        for default in (*arguments.defaults, *arguments.kw_defaults):
            if default is not None:
                self.visit(default)


# meiva:note
# One `L<line> <name>` per name the script reads but never binds.
#
# The conservative F821-shaped read: a name bound anywhere counts as bound, a
# `from x import *` leaves the file's name set open and disables the check. What
# remains is the class the import check cannot see — a statement reading a name
# nothing at all binds (#2678-adjacent; 2026-10-01 audit).
def undefined_name_problems(tree: ast.Module) -> list[str]:
    if any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in ast.walk(tree)
    ):
        return []
    reads = _NameReads()
    reads.visit(tree)
    bound = _bound_names(tree)
    unbound = {name: line for name, line in reads.first_line.items() if name not in bound}
    return [f"L{line} {name}" for name, line in sorted(unbound.items(), key=lambda item: item[1])]
