from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from meizel import allowances
from meizel.graph.model import Code, EnumSubsetUse, PurityItem
from meizel.language.purity_structure import (
    record_attr_write,
    record_enum_subsets,
    record_module_structure,
    record_nested_patch,
    record_params,
    record_pass_through,
)

__all__ = [
    "DEFAULT_IMPURE_CALLS",
    "DEFAULT_IMPURE_IMPORTS",
    "PurityState",
    "make_state",
    "record_assign",
    "record_attr_write",
    "record_call",
    "record_class",
    "record_enum_subsets",
    "record_function",
    "record_nested_patch",
]

_Dotted = Callable[[ast.expr], str | None]

DEFAULT_IMPURE_IMPORTS = frozenset(
    {
        "subprocess",
        "socket",
        "shutil",
        "tempfile",
        "signal",
        "multiprocessing",
        "ctypes",
        "fcntl",
        "pty",
        "termios",
        "psycopg",
        "psycopg_pool",
        "redis",
        "httpx",
        "requests",
        "urllib",
        "fastapi",
        "starlette",
        "uvicorn",
        "watchfiles",
        "watchdog",
        "playwright",
    }
)

_OS_ENVIRON = ".".join(("os", "environ"))
_OS_GETENV = ".".join(("os", "getenv"))

DEFAULT_IMPURE_CALLS = frozenset(
    {
        "time.time",
        "time.monotonic",
        "time.sleep",
        "datetime.now",
        "datetime.utcnow",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        _OS_ENVIRON,
        _OS_GETENV,
        "os.getcwd",
        "os.kill",
        "os.killpg",
        "open",
        "builtins.open",
        "io.open",
        "uuid.uuid4",
        "uuid.uuid1",
    }
)

_PATH_IO_METHODS = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "rmdir",
        "rename",
        "exists",
        "is_file",
        "is_dir",
        "stat",
    }
)
_PATH_AMBIGUOUS_METHODS = frozenset({"replace", "open"})
_CALL_ALIASES = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.utcnow": "datetime.utcnow",
    "builtins.open": "open",
    "io.open": "open",
}

_IMPURE_IMPORT_HINT = "move effectful imports to an adapter outside the strict core"
_IMPURE_CALL_HINT = "inject clock, environment, randomness, or I/O results as explicit parameters"
_ASYNC_HINT = (
    "keep strict modules synchronous and pure; move async orchestration to a service or adapter"
)
_UNFROZEN_HINT = "use `@dataclass(frozen=True)` so value objects in strict modules are immutable"
_UNTYPED_HINT = "annotate public parameters and return types with concrete types or typed dataclasses instead of `Any` or bare `dict`"


@dataclass(frozen=True, slots=True)
class PurityState:
    impure_imports: frozenset[str]
    impure_calls: frozenset[str]
    max_public_symbols: int = 6
    max_params: int = 6
    max_attr_hops: int = 3
    items: list[PurityItem] = field(default_factory=list[PurityItem])
    env_lines: set[int] = field(default_factory=set[int])
    path_vars: set[str] = field(default_factory=set[str])
    enum_subsets: list[EnumSubsetUse] = field(default_factory=list[EnumSubsetUse])
    defined_classes: set[str] = field(default_factory=set[str])

    def finalize_subsets(self) -> list[EnumSubsetUse]:
        return [u for u in self.enum_subsets if u.cls_name not in self.defined_classes]

    def record_module(self, tree: ast.Module) -> None:
        record_module_structure(self.items, tree, self.max_public_symbols, self.max_attr_hops)

    def record_import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".", 1)[0]
            if top in self.impure_imports:
                self.items.append(
                    PurityItem(
                        node.lineno,
                        Code.IMPURE_CALL,
                        f"import {top}",
                        f"impure import `{alias.name}` in strict module",
                        _IMPURE_IMPORT_HINT,
                    )
                )

    def record_import_from(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            top = node.module.split(".", 1)[0]
            if top in self.impure_imports:
                self.items.append(
                    PurityItem(
                        node.lineno,
                        Code.IMPURE_CALL,
                        f"import {top}",
                        f"impure import `{node.module}` in strict module",
                        _IMPURE_IMPORT_HINT,
                    )
                )

    def record_env_access(self, dotted_name: str | None, line: int) -> None:
        if (
            dotted_name is not None
            and (dotted_name == _OS_ENVIRON or dotted_name.startswith(f"{_OS_ENVIRON}."))
            and line not in self.env_lines
        ):
            self.env_lines.add(line)
            self.items.append(
                PurityItem(
                    line,
                    Code.IMPURE_CALL,
                    _OS_ENVIRON,
                    f"impure environment access `{_OS_ENVIRON}` in strict module",
                    _IMPURE_CALL_HINT,
                )
            )


def _policy_int(policy: allowances.Policy, section: str, default: int) -> int:
    vals = policy.names("purity", section)
    return int(vals[0]) if vals and vals[0].isdigit() else default


@cache
def _rules_for_root(root: Path) -> tuple[frozenset[str], frozenset[str], int, int, int]:
    path = root / allowances.POLICY
    if not path.is_file():
        return DEFAULT_IMPURE_IMPORTS, DEFAULT_IMPURE_CALLS, 6, 6, 3
    policy, _ = allowances.parse(path.read_text(encoding="utf-8"))
    imports = DEFAULT_IMPURE_IMPORTS | frozenset(policy.names("purity", "impure-imports"))
    calls = DEFAULT_IMPURE_CALLS | frozenset(policy.names("purity", "impure-calls"))
    return (
        imports,
        calls,
        _policy_int(policy, "max-public-symbols", 6),
        _policy_int(policy, "max-params", 6),
        _policy_int(policy, "max-attr-hops", 3),
    )


def make_state(root: Path) -> PurityState:
    imports, calls, max_pub, max_params, max_hops = _rules_for_root(root)
    return PurityState(imports, calls, max_pub, max_params, max_hops)


def _annotation_ast(ann: ast.expr) -> ast.AST:
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            return ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return ann
    return ann


def _is_path_annotation(ann: ast.expr) -> bool:
    parsed = _annotation_ast(ann)
    for sub in ast.walk(parsed):
        if isinstance(sub, ast.Name) and sub.id in {"Path", "PurePath", "PosixPath"}:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in {"Path", "PurePath", "PosixPath"}:
            return True
    return False


def _untyped_kind(ann: ast.expr) -> str | None:
    parsed = _annotation_ast(ann)
    subscripted = {id(s.value) for s in ast.walk(parsed) if isinstance(s, ast.Subscript)}
    for sub in ast.walk(parsed):
        if isinstance(sub, ast.Name) and sub.id == "Any":
            return "Any"
        if isinstance(sub, ast.Attribute) and sub.attr == "Any":
            return "Any"
    for sub in ast.walk(parsed):
        if id(sub) in subscripted:
            continue
        if isinstance(sub, ast.Name) and sub.id in {"dict", "Dict"}:
            return "dict"
        if isinstance(sub, ast.Attribute) and sub.attr in {"dict", "Dict"}:
            return "dict"
    return None


def record_function(
    state: PurityState,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    top_level: bool,
) -> None:
    args = node.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.annotation is not None and _is_path_annotation(arg.annotation):
            state.path_vars.add(arg.arg)
    if isinstance(node, ast.AsyncFunctionDef):
        state.items.append(
            PurityItem(
                node.lineno,
                Code.IMPURE_CALL,
                f"async def {node.name}",
                f"async function `{node.name}` in strict module",
                _ASYNC_HINT,
            )
        )
    record_params(state.items, node, state.max_params)
    if not top_level or node.name.startswith("_"):
        return
    record_pass_through(state.items, node)
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg):
        if arg is not None and arg.annotation is not None:
            bad = _untyped_kind(arg.annotation)
            if bad is not None:
                state.items.append(
                    PurityItem(
                        node.lineno,
                        Code.UNTYPED_EXPORT,
                        f"def {node.name}",
                        f"public function `{node.name}` parameter `{arg.arg}` uses untyped `{bad}` annotation",
                        _UNTYPED_HINT,
                    )
                )
    if node.returns is not None:
        bad = _untyped_kind(node.returns)
        if bad is not None:
            state.items.append(
                PurityItem(
                    node.lineno,
                    Code.UNTYPED_EXPORT,
                    f"def {node.name}",
                    f"public function `{node.name}` return type uses untyped `{bad}` annotation",
                    _UNTYPED_HINT,
                )
            )


def _is_dataclass_target(expr: ast.expr, dotted: _Dotted) -> bool:
    if dotted(expr) == "dataclasses.dataclass":
        return True
    return isinstance(expr, ast.Name) and expr.id == "dataclass"


def _has_unfrozen_dataclass(node: ast.ClassDef, dotted: _Dotted) -> bool:
    for deco in node.decorator_list:
        if isinstance(deco, ast.Call):
            if not _is_dataclass_target(deco.func, dotted):
                continue
            frozen = any(
                k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in deco.keywords
            )
            if not frozen:
                return True
        elif _is_dataclass_target(deco, dotted):
            return True
    return False


def record_class(state: PurityState, node: ast.ClassDef, dotted: _Dotted) -> None:
    state.defined_classes.add(node.name)
    if _has_unfrozen_dataclass(node, dotted):
        state.items.append(
            PurityItem(
                node.lineno,
                Code.UNFROZEN_VALUE,
                f"class {node.name}",
                f"@dataclass class `{node.name}` in strict module must set frozen=True",
                _UNFROZEN_HINT,
            )
        )


def _is_path_expr(expr: ast.expr, dotted: _Dotted, path_vars: set[str]) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in path_vars
    if isinstance(expr, ast.Call):
        target = dotted(expr.func)
        if target in {"pathlib.Path", "pathlib.PosixPath", "Path"}:
            return True
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in {
            "resolve",
            "with_name",
            "with_suffix",
            "expanduser",
        }:
            return _is_path_expr(expr.func.value, dotted, path_vars)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return _is_path_expr(expr.left, dotted, path_vars)
    if isinstance(expr, ast.Attribute) and expr.attr == "parent":
        return _is_path_expr(expr.value, dotted, path_vars)
    return False


def record_assign(state: PurityState, node: ast.Assign | ast.AnnAssign, dotted: _Dotted) -> None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if (
        len(targets) == 1
        and isinstance(targets[0], ast.Name)
        and node.value is not None
        and _is_path_expr(node.value, dotted, state.path_vars)
    ):
        state.path_vars.add(targets[0].id)


def record_call(
    state: PurityState,
    node: ast.Call,
    callee: str | None,
    dotted: _Dotted,
    *,
    unshadowed_open: bool,
) -> None:
    if unshadowed_open:
        state.items.append(
            PurityItem(
                node.lineno,
                Code.IMPURE_CALL,
                "open",
                "impure call `open` in strict module",
                _IMPURE_CALL_HINT,
            )
        )
        return
    if callee is not None:
        norm = _CALL_ALIASES.get(callee, callee)
        if (
            norm in state.impure_calls
            or callee in state.impure_calls
            or callee.startswith("random.")
        ):
            if norm == _OS_ENVIRON:
                state.env_lines.add(node.lineno)
            state.items.append(
                PurityItem(
                    node.lineno,
                    Code.IMPURE_CALL,
                    norm,
                    f"impure call `{norm}` in strict module",
                    _IMPURE_CALL_HINT,
                )
            )
            return
    if not isinstance(node.func, ast.Attribute):
        return
    attr = node.func.attr
    if (
        attr in _PATH_IO_METHODS and (callee is None or callee.startswith(("pathlib.", "Path.")))
    ) or (
        attr in _PATH_AMBIGUOUS_METHODS and _is_path_expr(node.func.value, dotted, state.path_vars)
    ):
        state.items.append(
            PurityItem(
                node.lineno,
                Code.IMPURE_CALL,
                f"Path.{attr}",
                f"impure filesystem call `Path.{attr}` in strict module",
                _IMPURE_CALL_HINT,
            )
        )
