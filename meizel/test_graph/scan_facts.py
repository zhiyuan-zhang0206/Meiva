from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeGuard

from meizel.test_graph.model import (
    FACT_DYNAMIC_FIXTURE,
    FACT_DYNAMIC_IMPORT,
    FACT_IMPORT_ONLY,
    FACT_PYTESTER_NATIVE,
    FACT_SYNTAX_ERROR,
    FileTestFacts,
    ModuleTable,
    RawFileTestFacts,
    TestCheckout,
)

__all__ = [
    "resolve_test_facts",
    "scan_raw_test_facts",
]

_DYN_MOD_CALLS = frozenset(
    {
        "importlib.import_module",
        "import_module",
        "__import__",
        "runpy.run_module",
        "pytest.importorskip",
        "importorskip",
    }
)
_DYN_FILE_CALLS = frozenset(
    {
        "runpy.run_path",
        "spec_from_file_location",
        "importlib.util.spec_from_file_location",
    }
)
_PYTESTER_NATIVE_TOKENS = (
    "provisioned_db",
    "provisioned_redis",
    "gateway_data_plane",
    "db_url",
    "database",
    "redis_url",
    "event_bus",
    "db_conn",
    "adb_conn",
    "aops_pool",
    "postgres_access",
    "redis_access",
    "containers",
)


def _is_obj_seq(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, (list, tuple))


def _is_str_seq(value: object) -> TypeGuard[list[str] | tuple[str, ...]]:
    return _is_obj_seq(value) and all(isinstance(item, str) for item in value)


@dataclass(frozen=True, slots=True)
class _Collector:
    path: str
    direct_imports: set[str] = field(default_factory=set[str])
    from_imports: list[tuple[int, str, tuple[tuple[str, str], ...]]] = field(
        default_factory=list[tuple[int, str, tuple[tuple[str, str], ...]]]
    )
    dynamic_modules: set[str] = field(default_factory=set[str])
    anchored_resources: set[str] = field(default_factory=set[str])
    fixtures: dict[str, tuple[bool, tuple[str, ...]]] = field(
        default_factory=dict[str, tuple[bool, tuple[str, ...]]]
    )
    test_requests: set[str] = field(default_factory=set[str])
    markers: set[str] = field(default_factory=set[str])
    plugins: list[str] = field(default_factory=list[str])
    unknown_sites: list[tuple[int, str, str]] = field(default_factory=list[tuple[int, str, str]])
    root_vars: dict[str, str] = field(default_factory=dict[str, str])


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        inner = func.value
        if isinstance(inner, ast.Name):
            return f"{inner.id}.{func.attr}"
        if isinstance(inner, ast.Attribute):
            root = inner.value
            if isinstance(root, ast.Name):
                return f"{root.id}.{inner.attr}.{func.attr}"
    return ""


def _is_docstring_stmt(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_dunder_all_assign(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return False
    target = stmt.targets[0]
    return (
        isinstance(target, ast.Name)
        and target.id == "__all__"
        and isinstance(stmt.value, (ast.List, ast.Tuple))
        and all(isinstance(elt, ast.Constant) for elt in stmt.value.elts)
    )


def _is_import_only_module(tree: ast.Module) -> bool:
    if not tree.body:
        return False
    for index, stmt in enumerate(tree.body):
        if index == 0 and _is_docstring_stmt(stmt):
            continue
        if isinstance(stmt, (ast.Import, ast.ImportFrom)) or _is_dunder_all_assign(stmt):
            continue
        return False
    return True


def _extract_mark(expr: ast.expr, marks: set[str], usefixtures: set[str]) -> None:
    if isinstance(expr, ast.Call):
        name = _call_name(expr.func)
        if name in {"pytest.mark.usefixtures", "mark.usefixtures"}:
            for arg in expr.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    usefixtures.add(arg.value)
        _extract_mark(expr.func, marks, usefixtures)
    elif isinstance(expr, ast.Attribute):
        inner = expr.value
        if (isinstance(inner, ast.Attribute) and inner.attr == "mark") or (
            isinstance(inner, ast.Name) and inner.id == "mark"
        ):
            marks.add(expr.attr)
    elif isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        for elt in expr.elts:
            _extract_mark(elt, marks, usefixtures)


def _decorator_tail_name(deco: ast.expr) -> str:
    target = deco.func if isinstance(deco, ast.Call) else deco
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _parametrize_arg_names(first: ast.expr) -> list[str]:
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return [part.strip() for part in first.value.split(",") if part.strip()]
    if isinstance(first, (ast.List, ast.Tuple)):
        return [
            elt.value
            for elt in first.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
    return []


def _constant_str_values(expr: ast.expr) -> tuple[str, ...] | None:
    if not isinstance(expr, (ast.List, ast.Tuple)) or not expr.elts:
        return None
    vals = [e.value for e in expr.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return tuple(vals) if len(vals) == len(expr.elts) else None


def _has_truthy_indirect(keywords: list[ast.keyword]) -> bool:
    return any(
        kw.arg == "indirect"
        and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
        for kw in keywords
    )


def _extract_parametrize_map(
    decorators: list[ast.expr],
) -> tuple[set[str], dict[str, tuple[str, ...]]]:
    direct_params: set[str] = set()
    finite_strings: dict[str, tuple[str, ...]] = {}
    for deco in decorators:
        if not isinstance(deco, ast.Call) or _decorator_tail_name(deco) != "parametrize":
            continue
        if not deco.args or _has_truthy_indirect(deco.keywords):
            continue
        names = _parametrize_arg_names(deco.args[0])
        direct_params.update(names)
        if len(names) == 1 and len(deco.args) >= 2:
            str_vals = _constant_str_values(deco.args[1])
            if str_vals is not None:
                finite_strings[names[0]] = str_vals
    return direct_params, finite_strings


def _count_injected_patch_args(decorators: list[ast.expr]) -> int:
    injected = 0
    for deco in decorators:
        if not isinstance(deco, ast.Call):
            continue
        cname = _call_name(deco.func)
        if any(kw.arg == "new" for kw in deco.keywords):
            continue
        if (cname.endswith("patch") and len(deco.args) < 2) or (
            cname.endswith("patch.object") and len(deco.args) < 3
        ):
            injected += 1
    return injected


def _param_fixtures(fn: ast.FunctionDef | ast.AsyncFunctionDef, excluded: set[str]) -> list[str]:
    fn_args = fn.args
    pos = [*fn_args.posonlyargs, *fn_args.args]
    req_pos = pos[: len(pos) - len(fn_args.defaults)]
    req_kw = [
        arg
        for arg, default in zip(fn_args.kwonlyargs, fn_args.kw_defaults, strict=True)
        if default is None
    ]
    params = [arg.arg for arg in (*req_pos, *req_kw)]
    if params and params[0] in {"self", "cls"}:
        params = params[1:]
    injected = _count_injected_patch_args(fn.decorator_list)
    return [param for param in params[injected:] if param not in excluded]


def _expand_fstring(expr: ast.JoinedStr, env: dict[str, tuple[str, ...]]) -> list[str] | None:
    prefixes = [""]
    for part in expr.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            prefixes = [f"{p}{part.value}" for p in prefixes]
        elif (
            isinstance(part, ast.FormattedValue)
            and part.conversion == -1
            and part.format_spec is None
            and isinstance(part.value, ast.Name)
            and part.value.id in env
        ):
            choices = env[part.value.id]
            prefixes = [f"{p}{choice}" for p in prefixes for choice in choices]
        else:
            return None
    return prefixes


def _parse_inline_imports(code: str) -> list[str]:
    try:
        sub = ast.parse(code)
    except SyntaxError:
        return []
    found: list[str] = []
    for node in ast.walk(sub):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
            for alias in node.names:
                if alias.name != "*":
                    found.append(f"{node.module}.{alias.name}")
    return found


# meiva:note
# Resolves `Path(__file__).resolve().parents[k]` or `Path(__file__).parents[k]` to a repo-relative
# directory prefix so downstream `/` path expressions record exact anchored resource edges.
def _resolve_file_parents_anchor(path: str, expr: ast.expr) -> str | None:
    parts = path.split("/")[:-1]
    if (
        isinstance(expr, ast.Subscript)
        and isinstance(expr.value, ast.Attribute)
        and expr.value.attr == "parents"
        and isinstance(expr.slice, ast.Constant)
        and isinstance(expr.slice.value, int)
    ):
        idx = expr.slice.value
        base_call = expr.value.value
        if _is_dunder_file_path(base_call) and 0 <= idx <= len(parts):
            kept = parts[: len(parts) - idx]
            return "/".join(kept)
    return None


def _is_dunder_file_path(expr: ast.expr) -> bool:
    curr = expr
    if (
        isinstance(curr, ast.Call)
        and isinstance(curr.func, ast.Attribute)
        and curr.func.attr == "resolve"
    ):
        curr = curr.func.value
    return (
        isinstance(curr, ast.Call)
        and _call_name(curr.func) in {"Path", "pathlib.Path"}
        and len(curr.args) == 1
        and isinstance(curr.args[0], ast.Name)
        and curr.args[0].id == "__file__"
    )


def _resolve_anchored_path(expr: ast.expr, roots: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Name) and expr.id in roots:
        return roots[expr.id]
    if (
        isinstance(expr, ast.BinOp)
        and isinstance(expr.op, ast.Div)
        and isinstance(expr.right, ast.Constant)
        and isinstance(expr.right.value, str)
    ):
        left = _resolve_anchored_path(expr.left, roots)
        if left is not None:
            seg = expr.right.value.strip("/")
            if not seg:
                return left
            return f"{left}/{seg}" if left else seg
    return None


def _scan_module_globals(tree: ast.Module, collector: _Collector) -> int:
    flags = 0
    mod_usefx: set[str] = set()
    for stmt in tree.body:
        targets: list[ast.expr] = []
        val: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
            val = stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
            val = stmt.value
        if val is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            anchor = _resolve_file_parents_anchor(collector.path, val)
            if anchor is not None:
                collector.root_vars[target.id] = anchor
            if target.id == "pytestmark":
                _extract_mark(val, collector.markers, mod_usefx)
            elif target.id == "pytest_plugins":
                flags |= _record_plugins(val, stmt.lineno, collector)
    collector.test_requests.update(mod_usefx)
    return flags


def _record_plugins(expr: ast.expr, lineno: int, collector: _Collector) -> int:
    try:
        evaluated: object = ast.literal_eval(expr)
    except (ValueError, SyntaxError):
        collector.unknown_sites.append((lineno, "non-literal pytest_plugins", ast.unparse(expr)))
        return FACT_DYNAMIC_IMPORT
    if isinstance(evaluated, str):
        collector.plugins.append(evaluated)
        return 0
    if _is_str_seq(evaluated):
        collector.plugins.extend(evaluated)
        return 0
    collector.unknown_sites.append((lineno, "non-string pytest_plugins", ast.unparse(expr)))
    return FACT_DYNAMIC_IMPORT


def _parse_fixture_keywords(keywords: list[ast.keyword], fx_name: str) -> tuple[bool, str]:
    autouse = False
    for kw in keywords:
        if kw.arg == "autouse" and (
            not isinstance(kw.value, ast.Constant) or kw.value.value is True
        ):
            autouse = True
        elif (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            fx_name = kw.value.value
    return autouse, fx_name


def _inspect_fixture_decorators(
    decorators: list[ast.expr],
    default_name: str,
    fn_marks: set[str],
    fn_usefx: set[str],
) -> tuple[bool, bool, str]:
    is_fixture = False
    autouse = False
    fx_name = default_name
    for deco in decorators:
        _extract_mark(deco, fn_marks, fn_usefx)
        if _decorator_tail_name(deco) != "fixture":
            continue
        is_fixture = True
        if isinstance(deco, ast.Call):
            call_auto, fx_name = _parse_fixture_keywords(deco.keywords, fx_name)
            autouse = autouse or call_auto
    return is_fixture, autouse, fx_name


def _record_local_fstring_assign(node: ast.AST, local_bindings: dict[str, tuple[str, ...]]) -> None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.JoinedStr)
    ):
        expanded = _expand_fstring(node.value, local_bindings)
        if expanded is not None:
            local_bindings[node.targets[0].id] = tuple(expanded)


def _scan_function_body(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    local_bindings: dict[str, tuple[str, ...]],
) -> tuple[int, set[str]]:
    flags = 0
    body_lookups: set[str] = set()
    for sub in ast.walk(stmt):
        _record_local_fstring_assign(sub, local_bindings)
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "getfixturevalue"
        ):
            if (
                sub.args
                and isinstance(sub.args[0], ast.Constant)
                and isinstance(sub.args[0].value, str)
            ):
                body_lookups.add(sub.args[0].value)
            else:
                flags |= FACT_DYNAMIC_FIXTURE
    return flags, body_lookups


def _inspect_function_def(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef,
    cls_usefx: set[str],
    collector: _Collector,
    fn_envs: dict[int, dict[str, tuple[str, ...]]],
) -> int:
    fn_marks: set[str] = set()
    fn_usefx: set[str] = set()
    is_fixture, autouse, fx_name = _inspect_fixture_decorators(
        stmt.decorator_list, stmt.name, fn_marks, fn_usefx
    )
    collector.markers.update(fn_marks)
    direct_params, finite_env = _extract_parametrize_map(stmt.decorator_list)
    local_bindings = dict(finite_env)
    flags, body_lookups = _scan_function_body(stmt, local_bindings)
    if local_bindings:
        for sub in ast.walk(stmt):
            fn_envs[id(sub)] = local_bindings
    params = _param_fixtures(stmt, direct_params)
    if is_fixture:
        reqs = tuple(dict.fromkeys([*params, *sorted(fn_usefx), *sorted(body_lookups)]))
        collector.fixtures[fx_name] = (autouse, reqs)
    elif stmt.name.startswith("test"):
        collector.test_requests.update((*params, *fn_usefx, *cls_usefx, *body_lookups))
    return flags


def _walk_definitions(
    stmts: list[ast.stmt],
    cls_usefx: set[str],
    collector: _Collector,
) -> tuple[int, dict[int, dict[str, tuple[str, ...]]]]:
    flags = 0
    fn_envs: dict[int, dict[str, tuple[str, ...]]] = {}
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            flags |= _inspect_function_def(stmt, cls_usefx, collector, fn_envs)
        elif isinstance(stmt, ast.ClassDef):
            c_marks: set[str] = set()
            c_usefx = set(cls_usefx)
            for deco in stmt.decorator_list:
                _extract_mark(deco, c_marks, c_usefx)
            for c_stmt in stmt.body:
                if isinstance(c_stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "pytestmark" for t in c_stmt.targets
                ):
                    _extract_mark(c_stmt.value, c_marks, c_usefx)
            collector.markers.update(c_marks)
            sub_flags, sub_envs = _walk_definitions(stmt.body, c_usefx, collector)
            flags |= sub_flags
            fn_envs.update(sub_envs)
    return flags, fn_envs


def _walk_expressions(
    tree: ast.Module,
    fn_envs: dict[int, dict[str, tuple[str, ...]]],
    collector: _Collector,
) -> int:
    flags = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                collector.direct_imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            pairs = tuple((alias.name, alias.asname or alias.name) for alias in node.names)
            collector.from_imports.append((node.level, node.module or "", pairs))
        elif isinstance(node, ast.Call):
            flags |= _inspect_call(node, fn_envs.get(id(node), {}), collector)
        elif isinstance(node, (ast.List, ast.Tuple)):
            _inspect_argv_sequence(node.elts, collector)
    return flags


_ANCHORED_READ_ATTRS = frozenset({"read_text", "read_bytes", "open", "rglob", "glob", "iterdir"})


def _resolve_dynamic_mod_targets(
    node: ast.Call, env: dict[str, tuple[str, ...]]
) -> tuple[str, ...] | None:
    if not node.args:
        return None
    arg0 = node.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return None if arg0.value.startswith(".") else (arg0.value,)
    if isinstance(arg0, ast.JoinedStr):
        expanded = _expand_fstring(arg0, env)
        if expanded is not None and all(not item.startswith(".") for item in expanded):
            return tuple(expanded)
        return None
    if isinstance(arg0, ast.Name) and arg0.id in env:
        choices = env[arg0.id]
        return choices if all(not item.startswith(".") for item in choices) else None
    return None


def _inspect_call(
    node: ast.Call,
    env: dict[str, tuple[str, ...]],
    collector: _Collector,
) -> int:
    cname = _call_name(node.func)
    if cname in _DYN_MOD_CALLS:
        targets = _resolve_dynamic_mod_targets(node, env)
        if targets is not None:
            collector.dynamic_modules.update(targets)
            return 0
        collector.unknown_sites.append((node.lineno, "opaque dynamic import", ast.unparse(node)))
        return FACT_DYNAMIC_IMPORT
    if cname in _DYN_FILE_CALLS:
        collector.unknown_sites.append((node.lineno, "opaque dynamic file load", ast.unparse(node)))
        return FACT_DYNAMIC_IMPORT
    if isinstance(node.func, ast.Attribute) and collector.root_vars:
        anchored = _resolve_anchored_path(node.func.value, collector.root_vars)
        if anchored is not None and node.func.attr in _ANCHORED_READ_ATTRS:
            collector.anchored_resources.add(anchored)
    return 0


def _inspect_argv_sequence(elts: list[ast.expr], collector: _Collector) -> None:
    for idx in range(len(elts) - 1):
        flag_elt = elts[idx]
        next_elt = elts[idx + 1]
        if (
            isinstance(flag_elt, ast.Constant)
            and isinstance(next_elt, ast.Constant)
            and isinstance(next_elt.value, str)
        ):
            if flag_elt.value == "-m" and not next_elt.value.startswith("-"):
                collector.dynamic_modules.add(next_elt.value)
            elif flag_elt.value == "-c":
                collector.dynamic_modules.update(_parse_inline_imports(next_elt.value))


# meiva:note
# Parses one Python source into layout-independent raw test/import facts without consulting the
# checkout's module table.
def scan_raw_test_facts(path: str, source: str) -> RawFileTestFacts:
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return RawFileTestFacts(
            path=path,
            direct_imports=(),
            from_imports=(),
            dynamic_modules=(),
            anchored_resources=(),
            fixtures=(),
            test_requests=(),
            markers=(),
            plugins=(),
            unknown_sites=((1, "syntax error", path),),
            flags=FACT_SYNTAX_ERROR,
        )
    collector = _Collector(path=path)
    flags = FACT_IMPORT_ONLY if _is_import_only_module(tree) else 0
    flags |= _scan_module_globals(tree, collector)
    def_flags, fn_envs = _walk_definitions(tree.body, set(), collector)
    flags |= def_flags
    flags |= _walk_expressions(tree, fn_envs, collector)
    all_fx_reqs = set(collector.test_requests)
    for _, reqs in collector.fixtures.values():
        all_fx_reqs.update(reqs)
    if "pytester" in all_fx_reqs and any(tok in source for tok in _PYTESTER_NATIVE_TOKENS):
        flags |= FACT_PYTESTER_NATIVE
    return RawFileTestFacts(
        path=path,
        direct_imports=tuple(sorted(collector.direct_imports)),
        from_imports=tuple(collector.from_imports),
        dynamic_modules=tuple(sorted(collector.dynamic_modules)),
        anchored_resources=tuple(sorted(collector.anchored_resources)),
        fixtures=tuple(sorted((k, v[0], v[1]) for k, v in collector.fixtures.items())),
        test_requests=tuple(sorted(collector.test_requests)),
        markers=tuple(sorted(collector.markers)),
        plugins=tuple(dict.fromkeys(collector.plugins)),
        unknown_sites=tuple(collector.unknown_sites),
        flags=flags,
    )


def _resolve_relative_module(file_path: str, level: int, mod: str) -> str | None:
    if level == 0:
        return mod or None
    anchor = file_path.split("/")[:-1]
    up = level - 1
    if up > len(anchor):
        return None
    kept = anchor[: len(anchor) - up]
    if mod:
        return ".".join([*kept, mod]) if kept else mod
    return ".".join(kept) if kept else None


def _resolve_from_import(
    rel: str,
    entry: tuple[int, str, tuple[tuple[str, str], ...]],
    tops: frozenset[str],
    table: ModuleTable,
) -> tuple[set[str], dict[str, str], list[tuple[str, str]]]:
    level, mod, pairs = entry
    imps: set[str] = set()
    reexp: dict[str, str] = {}
    sym_uses: list[tuple[str, str]] = []
    base = _resolve_relative_module(rel, level, mod)
    if base is None:
        return imps, reexp, sym_uses
    resolved_base = table.resolve_prefix(base, tops)
    if resolved_base is not None:
        imps.update(table.module_files(resolved_base, tops))
    src_file = table.file(resolved_base) if resolved_base is not None else None
    for orig_name, bound_name in pairs:
        if orig_name == "*":
            continue
        candidate = f"{base}.{orig_name}"
        if table.kind(candidate) is not None:
            imps.update(table.module_files(candidate, tops))
        elif src_file is not None:
            reexp[bound_name] = f"{src_file}:{orig_name}"
            sym_uses.append((src_file, orig_name))
    return imps, reexp, sym_uses


def _expand_loaded_dirs(load_dirs: tuple[str, ...], python_files: tuple[str, ...]) -> list[str]:
    return [
        py_file
        for load_dir in load_dirs
        for py_file in python_files
        if py_file == load_dir or py_file.startswith(f"{load_dir}/")
    ]


def _preliminary_file_imports(
    rel: str,
    raw: RawFileTestFacts,
    checkout: TestCheckout,
    table: ModuleTable,
    loads_map: Mapping[str, tuple[str, ...]],
) -> tuple[set[str], dict[str, str], list[tuple[str, str]]]:
    tops = checkout.top_packages
    imps: set[str] = set()
    reexp: dict[str, str] = {}
    sym_uses: list[tuple[str, str]] = []

    for dotted in (*raw.direct_imports, *raw.dynamic_modules, *raw.plugins):
        resolved = table.resolve_prefix(dotted, tops)
        if resolved is not None:
            imps.update(table.module_files(resolved, tops))

    for entry in raw.from_imports:
        sub_imps, sub_reexp, sub_syms = _resolve_from_import(rel, entry, tops, table)
        imps.update(sub_imps)
        reexp.update(sub_reexp)
        sym_uses.extend(sub_syms)

    if rel in loads_map:
        imps.update(_expand_loaded_dirs(loads_map[rel], checkout.python_files))
    return imps, reexp, sym_uses


# meiva:note
# Resolves raw per-file AST facts against the checkout's module table, `[loads]` rules, and
# two-hop re-export chains.
def resolve_test_facts(
    checkout: TestCheckout,
    raw_by_file: Mapping[str, RawFileTestFacts],
    table: ModuleTable,
) -> dict[str, FileTestFacts]:
    loads_map = dict(checkout.policy_loads)
    prelim_imports: dict[str, set[str]] = {}
    reexports_by_file: dict[str, dict[str, str]] = {}
    symbol_imports: dict[str, list[tuple[str, str]]] = {}

    for rel, raw in raw_by_file.items():
        imps, reexp, sym_uses = _preliminary_file_imports(rel, raw, checkout, table, loads_map)
        prelim_imports[rel] = imps
        reexports_by_file[rel] = reexp
        symbol_imports[rel] = sym_uses

    def trace_reexport(src_file: str, sym_name: str) -> str | None:
        seen: set[tuple[str, str]] = set()
        curr_file, curr_sym = src_file, sym_name
        while (curr_file, curr_sym) not in seen:
            seen.add((curr_file, curr_sym))
            target = reexports_by_file.get(curr_file, {}).get(curr_sym)
            if target is None:
                return curr_file
            curr_file, curr_sym = target.split(":", 1)
        return None

    resolved_out: dict[str, FileTestFacts] = {}
    for rel, raw in raw_by_file.items():
        imps = set(prelim_imports[rel])
        for src_file, sym_name in symbol_imports[rel]:
            origin = trace_reexport(src_file, sym_name)
            if origin is not None:
                imps.add(origin)
        flags = raw.flags
        in_tests = "tests" in rel.split("/")[:-1]
        if rel in loads_map or (rel in checkout.policy_external and not in_tests):
            flags &= ~FACT_DYNAMIC_IMPORT
            unknowns = ()
        else:
            unknowns = raw.unknown_sites
        resolved_out[rel] = FileTestFacts(
            path=rel,
            imports=tuple(sorted(imps)),
            resources=raw.anchored_resources,
            reexports=tuple(sorted(reexports_by_file[rel].items())),
            fixtures=raw.fixtures,
            test_requests=raw.test_requests,
            markers=raw.markers,
            plugins=raw.plugins,
            unknown_sites=unknowns,
            flags=flags,
        )
    return resolved_out
