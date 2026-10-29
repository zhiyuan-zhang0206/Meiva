"""The names a first-party module binds at its top level, found without importing it."""

from __future__ import annotations

import ast
import builtins
import types
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache

from meizel.graph.imports import Scope, collect_scope, dotted_chain
from meizel.graph.universe import Universe

__all__ = [
    "Bindings",
    "ClassFacts",
    "find_class",
    "is_imported_binding",
    "module_bindings",
    "overridable_methods",
    "provides",
    "unambiguous_function_lines",
]

# Names every module object has, and the builtins its code resolves when it binds no such name.
_ALWAYS = frozenset(dir(types.ModuleType("m"))) | frozenset(dir(builtins))
_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_ABSTRACT_BASES = frozenset({"Protocol", "ABC"})
_REEXPORT_HOPS = 6


# meiva:note
# A class defined at the top level of a module: what it defines, whether it is abstract
# (`Protocol` or `ABC` among its bases, an `ABCMeta` metaclass, or an `@abstractmethod`), and the
# dotted origin of each base as the defining module's imports name it.
@dataclass(frozen=True, slots=True)
class ClassFacts:
    module: str
    name: str
    methods: frozenset[str]
    abstract: bool
    bases: tuple[str, ...]
    file: str


@dataclass(frozen=True, slots=True)
class Bindings:
    """The top-level names of one module, and whether it can serve others at run time."""

    names: frozenset[str]
    dynamic: bool
    defined: frozenset[str] = frozenset()
    imported: frozenset[str] = frozenset()
    all_names: frozenset[str] = frozenset()
    classes: tuple[ClassFacts, ...] = ()
    reexports: tuple[tuple[str, str], ...] = ()


def provides(bindings: Bindings, name: str) -> bool:
    """Whether setting or deleting `name` on the module acts on something that exists."""
    return bindings.dynamic or name in bindings.names or name in _ALWAYS


def is_imported_binding(bindings: Bindings, name: str) -> bool:
    return (
        not bindings.dynamic
        and name in bindings.imported
        and name not in bindings.defined
        and name not in bindings.all_names
    )


# meiva:note
# The bindings of the tracked `file` in `universe`; an untracked, unreadable or unparsable file is
# treated as dynamic.
def module_bindings(universe: Universe, file: str) -> Bindings:
    if file not in universe.files:
        return Bindings(frozenset(), dynamic=True)
    try:
        source = universe.repo.load_text(file)
    except (OSError, ValueError):
        return Bindings(frozenset(), dynamic=True)
    return _read(source, file)


@cache
def _read(source: str, file: str) -> Bindings:
    try:
        tree = ast.parse(source, filename=file)
    except (SyntaxError, ValueError):
        return Bindings(frozenset(), dynamic=True)
    defined: set[str] = set()
    imported: set[str] = set()
    all_names: set[str] = set()
    dynamic = False
    for node in _in_module_scope(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            imported.update(_names_bound(node))
        else:
            defined.update(_names_bound(node))
        all_names.update(_all_entries(node))
        dynamic |= _serves_names(node)
    if file.endswith("__init__.py"):
        defined.add("__path__")  # the import system sets it on a package
    names = frozenset(defined | imported)
    scope = Scope(None)
    collect_scope(scope, tree.body, file.split("/")[:-1], walrus=False)
    return Bindings(
        names,
        dynamic,
        defined=frozenset(defined),
        imported=frozenset(imported),
        all_names=frozenset(all_names),
        classes=_class_facts(tree, scope, file),
        reexports=tuple(scope.imports.items()),
    )


def unambiguous_function_lines(tree: ast.Module) -> frozenset[int]:
    counts: dict[str, int] = {}
    functions: dict[str, int] = {}
    for node in _in_module_scope(tree):
        if _serves_names(node):
            return frozenset()
        names = _names_bound(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Del):
            names = [node.id]
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions[node.name] = node.lineno
    return frozenset(line for name, line in functions.items() if counts[name] == 1)


def _all_entries(node: ast.AST) -> list[str]:
    value: ast.expr | None = None
    if (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    ) or (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "__all__"
    ):
        value = node.value
    if isinstance(value, ast.List | ast.Tuple | ast.Set):
        return [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _names_bound(node: ast.AST) -> list[str]:
    """The module-level names one node binds."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name, *_declared_global(node)]
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, ast.Import):
        return [(a.asname or a.name).split(".")[0] for a in node.names]
    if isinstance(node, ast.ImportFrom):
        return [a.asname or a.name for a in node.names]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _serves_names(node: ast.AST) -> bool:
    """Whether one node lets the module answer for names it does not bind."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name == "__getattr__"
    if isinstance(node, ast.ImportFrom):
        return any(a.name == "*" for a in node.names)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "globals"
    return False


def _in_module_scope(tree: ast.Module) -> Iterator[ast.AST]:
    """Every node that runs in the module's own namespace: nested scopes are yielded, not entered."""
    stack: list[ast.AST] = list(reversed(tree.body))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _NESTED):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _declared_global(function: ast.AST) -> Iterator[str]:
    """Names a function declares `global`: it binds them in the module when it runs."""
    if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
        return
    for node in ast.walk(function):
        if isinstance(node, ast.Global):
            yield from node.names


def _module_name(file: str) -> str:
    parts = file.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _tail(expr: ast.expr) -> str:
    target = expr.value if isinstance(expr, ast.Subscript) else expr
    chain = dotted_chain(target)
    return "" if chain is None else (chain[1] or [chain[0].id])[-1]


def _base_origin(scope: Scope, expr: ast.expr, module: str, local: frozenset[str]) -> str | None:
    target = expr.value if isinstance(expr, ast.Subscript) else expr
    chain = dotted_chain(target)
    if chain is None:
        return None
    base, attrs = chain
    origin, _ = scope.find(base.id)
    if origin is None and base.id in local:
        origin = f"{module}.{base.id}"
    return None if origin is None else ".".join([origin, *attrs])


def _is_abstract(node: ast.ClassDef) -> bool:
    if any(_tail(base) in _ABSTRACT_BASES for base in node.bases):
        return True
    if any(k.arg == "metaclass" and _tail(k.value) == "ABCMeta" for k in node.keywords):
        return True
    return any(
        isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(_tail(decorator) == "abstractmethod" for decorator in stmt.decorator_list)
        for stmt in node.body
    )


def _class_facts(tree: ast.Module, scope: Scope, file: str) -> tuple[ClassFacts, ...]:
    module = _module_name(file)
    defined = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    local = frozenset(node.name for node in defined)
    facts: list[ClassFacts] = []
    for node in defined:
        methods = frozenset(
            stmt.name
            for stmt in node.body
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
            and not (stmt.name.startswith("__") and stmt.name.endswith("__"))
        )
        bases = tuple(
            origin
            for base in node.bases
            if (origin := _base_origin(scope, base, module, local)) is not None
        )
        facts.append(ClassFacts(module, node.name, methods, _is_abstract(node), bases, file))
    return tuple(facts)


# meiva:note
# The class a dotted name refers to, wherever a chain of re-exports defines it. None when the
# name is not a first-party class: an unknown module, a name the module does not define, or a
# chain of re-exports longer than `hops`.
def find_class(universe: Universe, dotted: str, hops: int = _REEXPORT_HOPS) -> ClassFacts | None:
    if hops == 0 or not universe.is_first_party_name(dotted):
        return None
    owner, _, name = dotted.rpartition(".")
    module = universe.resolve_module(owner) if owner else None
    if module is None or module.file is None:
        return None
    read = module_bindings(universe, module.file)
    for facts in read.classes:
        if facts.name == name:
            return facts
    origin = dict(read.reexports).get(name)
    return None if origin is None or origin == dotted else find_class(universe, origin, hops - 1)


# meiva:note
# The methods a class defines or inherits from first-party classes (dunders excluded).
def overridable_methods(universe: Universe, facts: ClassFacts) -> frozenset[str]:
    found = set(facts.methods)
    pending = [(base, _REEXPORT_HOPS) for base in facts.bases]
    while pending:
        base, hops = pending.pop()
        parent = find_class(universe, base)
        if parent is not None and hops > 0:
            found |= parent.methods
            pending.extend((origin, hops - 1) for origin in parent.bases)
    return frozenset(found)
