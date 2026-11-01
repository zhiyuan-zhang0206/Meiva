"""pytest fixture definitions and requests, and their resolution to a defining file."""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from meizel.graph.model import Fact, FactKind
from meizel.graph.universe import Universe, parent_of

__all__ = [
    "DEFAULT_IMPURE_FIXTURES",
    "FixtureFunction",
    "FixtureIndex",
    "FixtureRequest",
    "FixtureState",
    "fixture_facts",
    "is_test_file",
    "record_function",
]

DEFAULT_IMPURE_FIXTURES = frozenset(
    {
        "db_conn",
        "database_gate",
        "database",
        "operator_database",
        "aops_pool",
        "pool",
        "event_bus",
        "redis_url",
        "monkeypatch",
    }
)

_FIXTURE_DECORATORS = frozenset({"pytest.fixture", "pytest_asyncio.fixture"})
_PARAMETRIZE = "pytest.mark.parametrize"
_PATCH_DECORATORS = {"unittest.mock.patch": 2, "unittest.mock.patch.object": 3}
_Dotted = Callable[[ast.expr], str | None]


@dataclass(frozen=True, slots=True)
class FixtureRequest:
    name: str
    line: int
    skip_self: bool = False
    requester_line: int = 0
    form: Literal["parameter", "usefixtures", "call", "lookup"] = "parameter"


@dataclass(frozen=True, slots=True)
class FixtureFunction:
    name: str
    line: int
    registered: str | None
    module_owned: bool
    parameters_known: bool


@dataclass(frozen=True, slots=True)
class FixtureState:
    """Fixture facts of one file, gathered while it is scanned."""

    defs: set[str] = field(default_factory=set[str])
    requests: list[FixtureRequest] = field(default_factory=list[FixtureRequest])
    plugins: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    functions: list[FixtureFunction] = field(default_factory=list[FixtureFunction])


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if name.endswith((".ts", ".tsx", ".mts", ".cts")):
        return (
            name.endswith(
                (
                    ".test.ts",
                    ".test.tsx",
                    ".spec.ts",
                    ".spec.tsx",
                    "-test-mock.ts",
                    "-test-mock.tsx",
                    ".test.mts",
                    ".test.cts",
                    ".spec.mts",
                    ".spec.cts",
                    "-test-mock.mts",
                    "-test-mock.cts",
                )
            )
            or "tests" in parts[:-1]
            or "test-support" in parts[:-1]
        )
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def _string_args(call: ast.Call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def fixture_name(node: ast.FunctionDef | ast.AsyncFunctionDef, dotted: _Dotted) -> str | None:
    """The name a `@pytest.fixture` function registers, or None for a plain function."""
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if dotted(target) not in _FIXTURE_DECORATORS:
            continue
        if isinstance(deco, ast.Call):
            for keyword in deco.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
        return node.name
    return None


def _defaultless(args: ast.arguments) -> list[str]:
    positional = [*args.posonlyargs, *args.args]
    required = positional[: len(positional) - len(args.defaults)]
    keyword = [a for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None]
    return [a.arg for a in (*required, *keyword)]


def _parametrized(deco: ast.Call) -> set[str]:
    if any(k.arg == "indirect" for k in deco.keywords) or not deco.args:
        return set()
    first = deco.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return {n.strip() for n in first.value.split(",") if n.strip()}
    if isinstance(first, ast.List | ast.Tuple):
        return {
            e.value for e in first.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
    return set()


def _patch_injected(deco: ast.Call, callee: str) -> bool:
    """Whether a `patch` decorator passes a mock as an argument (no explicit `new`)."""
    return len(deco.args) < _PATCH_DECORATORS[callee] and not any(
        k.arg == "new" for k in deco.keywords
    )


# meiva:note
# Parameters that pytest resolves as fixtures.
def _requested_names(node: ast.FunctionDef | ast.AsyncFunctionDef, dotted: _Dotted) -> list[str]:
    params = _defaultless(node.args)
    excluded: set[str] = set()
    injected = 0
    for deco in node.decorator_list:
        if not isinstance(deco, ast.Call):
            continue
        callee = dotted(deco.func)
        if callee == _PARAMETRIZE:
            excluded |= _parametrized(deco)
        elif callee in _PATCH_DECORATORS and _patch_injected(deco, callee):
            injected += 1
    if params and params[0] in {"self", "cls"}:
        params = params[1:]
    return [p for p in params[injected:] if p not in excluded]


def _literal_registration(node: ast.FunctionDef | ast.AsyncFunctionDef, dotted: _Dotted) -> bool:
    for deco in node.decorator_list:
        if not isinstance(deco, ast.Call) or dotted(deco.func) not in _FIXTURE_DECORATORS:
            continue
        if deco.args:
            return False
        for keyword in deco.keywords:
            if keyword.arg is None:
                return False
            if keyword.arg == "name" and not (
                isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)
            ):
                return False
    return True


def _parameters_known(node: ast.FunctionDef | ast.AsyncFunctionDef, dotted: _Dotted) -> bool:
    for deco in node.decorator_list:
        if not isinstance(deco, ast.Call) or dotted(deco.func) != _PARAMETRIZE:
            continue
        if any(keyword.arg is None for keyword in deco.keywords):
            return False
        indirect = next((k.value for k in deco.keywords if k.arg == "indirect"), None)
        if isinstance(indirect, ast.Constant) and indirect.value is True:
            continue
        if indirect is not None or not deco.args or not _literal_names(deco.args[0]):
            return False
    return True


def _literal_names(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str)
    return isinstance(value, ast.List | ast.Tuple) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str) for item in value.elts
    )


def record_function(
    state: FixtureState,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    dotted: _Dotted,
    *,
    test_file: bool,
    top_level: bool,
    module_owned: bool,
) -> None:
    registered = fixture_name(node, dotted)
    if registered is not None:
        state.defs.add(registered)
    is_test = test_file and top_level and node.name.startswith("test")
    if registered is None and not is_test:
        return
    state.functions.append(
        FixtureFunction(
            node.name,
            node.lineno,
            registered,
            module_owned and _literal_registration(node, dotted),
            _parameters_known(node, dotted),
        )
    )
    for name in _requested_names(node, dotted):
        state.requests.append(
            FixtureRequest(
                name, node.lineno, skip_self=name == registered, requester_line=node.lineno
            )
        )


def record_call(
    state: FixtureState,
    node: ast.Call,
    callee: str | None,
    *,
    requester: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> None:
    """`usefixtures("a")` and `request.getfixturevalue("a")` request by literal name."""
    owner = requester.lineno if requester is not None else 0
    marker = requester is not None and any(node is deco for deco in requester.decorator_list)
    if callee == "pytest.mark.usefixtures":
        for name in _string_args(node):
            state.requests.append(
                FixtureRequest(
                    name,
                    node.lineno,
                    requester_line=owner,
                    form="usefixtures" if marker else "call",
                )
            )
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "getfixturevalue":
        for name in _string_args(node)[:1]:
            state.requests.append(
                FixtureRequest(name, node.lineno, requester_line=owner, form="lookup")
            )


def plugin_names(value: ast.expr) -> list[str] | None:
    """Literal `pytest_plugins` entries; None when the value is not a literal."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.List | ast.Tuple):
        names = [
            e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        return names if len(names) == len(value.elts) else None
    return None


@dataclass(frozen=True, slots=True)
class FixtureIndex:
    """What each scanned file defines, for resolving a request to its definer."""

    universe: Universe
    defined: dict[str, set[str]]
    plugins: dict[str, list[str]]
    _plugin_cache: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]], init=False, repr=False, compare=False
    )

    def names(self, path: str) -> set[str]:
        return self.defined.get(path) or set()

    def plugin_files(self, path: str, seen: set[str] | None = None) -> list[str]:
        if seen is None and path in self._plugin_cache:
            return self._plugin_cache[path]
        top = seen is None
        active: set[str] = set() if seen is None else seen
        found: list[str] = []
        for dotted in self.plugins.get(path, ()):
            module = self.universe.resolve_module(dotted)
            if module is None or module.file is None or module.file in active:
                continue
            active.add(module.file)
            found.append(module.file)
            found.extend(self.plugin_files(module.file, active))
        if top:
            self._plugin_cache[path] = found
        return found

    def candidates(self, name: str, test_path: str) -> tuple[list[str], frozenset[str]]:
        visible = [test_path]
        directory = parent_of(test_path)
        while True:
            conftest = f"{directory}/conftest.py" if directory else "conftest.py"
            if conftest != test_path and conftest in self.defined:
                visible.append(conftest)
            if not directory:
                break
            directory = parent_of(directory)
        providers = [path for path in visible if name in self.names(path)]
        plugins = list(
            dict.fromkeys(
                plugin
                for path in visible
                for plugin in self.plugin_files(path)
                if plugin not in visible and name in self.names(plugin)
            )
        )
        return providers + plugins, frozenset(plugins) if len(plugins) > 1 else frozenset()

    def definer(self, request: FixtureRequest, src: str) -> str | None:
        """The file pytest takes `request` from; None when local or not first-party."""
        if not request.skip_self:
            if request.name in self.names(src):
                return None
            for plugin in self.plugin_files(src):
                if request.name in self.names(plugin):
                    return plugin
        directory = parent_of(src)
        if request.skip_self and src.endswith("conftest.py"):
            directory = parent_of(directory)
        return self._walk_up(request.name, directory, src)

    def _walk_up(self, name: str, directory: str, src: str) -> str | None:
        while True:
            conftest = f"{directory}/conftest.py" if directory else "conftest.py"
            if conftest in self.defined and conftest != src:
                if name in self.names(conftest):
                    return conftest
                for plugin in self.plugin_files(conftest):
                    if name in self.names(plugin):
                        return plugin
            if not directory:
                return None
            directory = parent_of(directory)


def fixture_facts(index: FixtureIndex, requests: dict[str, list[FixtureRequest]]) -> list[Fact]:
    facts: list[Fact] = []
    for src, items in requests.items():
        for request in items:
            target = index.definer(request, src)
            if target is not None and target != src:
                facts.append(
                    Fact(
                        src,
                        target,
                        request.line,
                        FactKind.FIXTURE,
                        (request.name,),
                        f"fixture {request.name}",
                    )
                )
    return facts
