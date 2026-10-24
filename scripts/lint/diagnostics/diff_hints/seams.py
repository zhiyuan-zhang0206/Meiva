# meiva:note
# Hints about how a test controls what it tests. `test-seam` (`patch_shapes`) finds the
# patch shapes that are not `monkeypatch.setattr`: a rewritten module table or instance
# dictionary, an attribute assigned on an imported name or on a fixture object, a method
# replaced by a function, a mock's `side_effect`, a write to the process environment, a
# patch that tolerates a missing target, a mock transport, an assertion on source text
# and a real sleep in a test. `test-subclass` (`double_subclasses`) finds a test double
# made by subclassing production code or a database or cache client, and `spy-assert` a mock call
# assertion. All three look at test files only.
from __future__ import annotations

import ast

from scripts.lint.diagnostics.diff_hints.model import FileKind, FunctionNode, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.names import OS_ENVIRON, dotted
from scripts.lint.diagnostics.diff_hints.walk import own_nodes, scope_nodes

_OBJECT_PARAMS_SKIPPED = frozenset(
    {"self", "cls", "monkeypatch", "tmp_path", "tmp_path_factory", "capsys", "caplog", "request"}
)
_ENVIRON_WRITERS = frozenset({"update", "pop", "setdefault", "clear", "__setitem__"})
_SPY_PREFIXES = ("assert_called", "assert_awaited", "assert_not_called", "assert_not_awaited")
_SPY_NAMES = frozenset(
    {
        "assert_has_calls",
        "assert_any_call",
        "assert_has_awaits",
        "assert_any_await",
        "call_args",
        "call_args_list",
        "call_count",
        "mock_calls",
        "method_calls",
        "await_args",
        "await_args_list",
        "await_count",
    }
)
_RESOURCE_PACKAGES = frozenset({"psycopg", "psycopg_pool", "redis"})


def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AugAssign | ast.AnnAssign):
        return [node.target]
    return []


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_test_module(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def _named_call_seam(callee: str | None, unit: Unit, *, polling: bool) -> str | None:
    if callee == "httpx.MockTransport":
        return "`httpx.MockTransport` replaces the network instead of serving a real listener"
    if callee == "inspect.getsource":
        return "`inspect.getsource` asserts on source text instead of behavior"
    if callee == "time.sleep" and _is_test_module(unit.path) and not polling:
        return "`time.sleep` waits out a time window on the real clock"
    if callee is not None and callee.startswith(OS_ENVIRON + ".") and _is_test_module(unit.path):
        written = callee.rsplit(".", 1)[-1] in _ENVIRON_WRITERS
        return "writes the process environment directly" if written else None
    return None


def _method_call_seam(node: ast.Call, unit: Unit) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if (
        func.attr == "__setattr__"
        and isinstance(func.value, ast.Name)
        and func.value.id == "object"
    ):
        return "`object.__setattr__` writes around a frozen value"
    first = dotted(node.args[0], unit.names) if node.args else None
    if func.attr == "setitem" and first == "sys.modules":
        return "`setitem(sys.modules, ...)` swaps a module"
    return None


def _call_seam(node: ast.Call, unit: Unit, *, polling: bool) -> str | None:
    named = _named_call_seam(dotted(node.func, unit.names), unit, polling=polling)
    return named if named is not None else _method_call_seam(node, unit)


def _subscript_seam(target: ast.Subscript, unit: Unit) -> str | None:
    holder = target.value
    name = dotted(holder, unit.names)
    if name == "sys.modules":
        return "`sys.modules[...] = ...` swaps a module"
    if name == OS_ENVIRON and _is_test_module(unit.path):
        return "writes the process environment directly"
    if (
        isinstance(holder, ast.Call)
        and isinstance(holder.func, ast.Name)
        and holder.func.id == "vars"
    ):
        return "`vars(obj)[...] = ...` writes an instance attribute around the signature"
    if isinstance(holder, ast.Attribute) and holder.attr == "__dict__":
        return "`obj.__dict__[...] = ...` writes an instance attribute around the signature"
    return None


def _replaces_method(value: ast.expr | None, functions_here: frozenset[str]) -> bool:
    if isinstance(value, ast.Lambda):
        return True
    return isinstance(value, ast.Name) and value.id in functions_here


def _attribute_seam(
    node: ast.AST,
    target: ast.Attribute,
    unit: Unit,
    scope_names: tuple[frozenset[str], frozenset[str]],
) -> str | None:
    local_names, test_params = scope_names
    if target.attr == "side_effect":
        return "`.side_effect = ...` scripts a mock"
    base = target.value
    root = base
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(root, ast.Name) and root.id in unit.names and root.id not in local_names:
        return f"assigns `{ast.unparse(target)}` on an imported name"
    if not isinstance(base, ast.Name):
        return None
    value = getattr(node, "value", None)
    if base.id not in {"self", "cls"} and _replaces_method(value, _module_functions(unit)):
        return f"replaces `{base.id}.{target.attr}` with a function"
    if base.id in test_params:
        return f"assigns `{base.id}.{target.attr}` on a fixture object"
    return None


def _module_functions(unit: Unit) -> frozenset[str]:
    return frozenset(
        stmt.name
        for stmt in unit.tree.body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _local_names(function: FunctionNode | None) -> frozenset[str]:
    if function is None:
        return frozenset()
    names = {arg.arg for arg in (*function.args.posonlyargs, *function.args.args)}
    names |= {arg.arg for arg in function.args.kwonlyargs}
    for node in own_nodes(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return frozenset(names)


def _test_params(function: FunctionNode | None) -> frozenset[str]:
    if function is None or not function.name.startswith("test_"):
        return frozenset()
    args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    return frozenset(arg.arg for arg in args) - _OBJECT_PARAMS_SKIPPED


def _node_seams(
    node: ast.AST,
    unit: Unit,
    scope_names: tuple[frozenset[str], frozenset[str]],
    polled: frozenset[int],
) -> list[str]:
    found: list[str] = []
    if isinstance(node, ast.Call):
        call = _call_seam(node, unit, polling=id(node) in polled)
        if call is not None:
            found.append(call)
    for target in _targets(node):
        if isinstance(target, ast.Subscript):
            seam = _subscript_seam(target, unit)
        elif isinstance(target, ast.Attribute):
            seam = _attribute_seam(node, target, unit, scope_names)
        else:
            seam = None
        if seam is not None:
            found.append(seam)
    return found


def _raising_false(unit: Unit) -> list[Site]:
    detail = "`raising=False` lets a patch miss its target silently"
    return [
        Site(HintId.TEST_SEAM, unit.path, keyword.value.lineno, detail)
        for call in unit.calls
        for keyword in call.keywords
        if keyword.arg == "raising"
        and _is_false(keyword.value)
        and keyword.value.lineno in unit.added
    ]


# meiva:note
# The calls that sit inside a loop of their scope. A sleep there paces a poll for a real
# resource, which is the form the red line prefers; a sleep outside a loop waits out a
# time window that a value (`now`) could set.
def _loop_calls(nodes: list[ast.AST]) -> frozenset[int]:
    return frozenset(
        id(inner)
        for node in nodes
        if isinstance(node, ast.For | ast.While | ast.AsyncFor)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
    )


def patch_shapes(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.TEST:
        return []
    scopes: list[tuple[FunctionNode | None, list[ast.AST]]] = [
        (None, list(scope_nodes(unit.tree.body)))
    ]
    scopes.extend((function, list(own_nodes(function))) for function in unit.functions)
    found: dict[tuple[int, str], Site] = {}
    for function, nodes in scopes:
        scope_names = (_local_names(function), _test_params(function))
        polled = _loop_calls(nodes)
        for node in nodes:
            line = getattr(node, "lineno", 0)
            if line not in unit.added:
                continue
            for detail in _node_seams(node, unit, scope_names, polled):
                found[(line, detail)] = Site(HintId.TEST_SEAM, unit.path, line, detail)
    for site in _raising_false(unit):
        found[(site.line, site.detail)] = site
    return list(found.values())


def _base_kind(origin: str, unit: Unit) -> str | None:
    segments = origin.split(".")
    top = segments[0]
    if top in unit.roles.first_party:
        return None if "tests" in segments else "production"
    return "resource-client" if top in _RESOURCE_PACKAGES else None


def double_subclasses(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.TEST:
        return []
    sites: list[Site] = []
    for node in unit.classes:
        if node.lineno not in unit.added:
            continue
        if not any(isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) for stmt in node.body):
            continue
        for base in node.bases:
            origin = dotted(base.value if isinstance(base, ast.Subscript) else base, unit.names)
            kind = _base_kind(origin, unit) if origin else None
            if kind is not None and not (origin and origin.endswith(("Error", "Exception"))):
                detail = f"`{node.name}` subclasses {kind} class `{origin}` in a test"
                sites.append(Site(HintId.TEST_SUBCLASS, unit.path, node.lineno, detail))
    return sites


def spy_assert(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.TEST:
        return []
    lines: dict[int, str] = {}
    for node in unit.touched:
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr.startswith(_SPY_PREFIXES) or node.attr in _SPY_NAMES:
            lines.setdefault(node.lineno, node.attr)
    return [
        Site(HintId.SPY_ASSERT, unit.path, line, f"`.{attr}` asserts how a mock was called")
        for line, attr in lines.items()
    ]
