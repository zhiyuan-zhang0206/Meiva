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
    "invalidate_policy_cache",
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
        "os.stat",
        "os.lstat",
        "os.fstat",
        "os.open",
        "os.read",
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.readlink",
        "os.path.exists",
        "os.path.isfile",
        "os.path.isdir",
        "os.path.islink",
        "os.path.getsize",
        "os.path.getmtime",
        "os.path.realpath",
        "os.path.lexists",
        "os.path.getatime",
        "os.path.getctime",
        "os.path.samefile",
        "os.path.ismount",
        "os.path.abspath",
        "os.path.relpath",
        "os.path.expanduser",
        "os.path.expandvars",
        "os.statvfs",
        "os.fwalk",
        "os.getcwdb",
        "os.access",
        "os.chdir",
        "os.umask",
        "os.fdopen",
        "os.write",
        "os.close",
        "os.fsync",
        "os.pipe",
        "os.dup",
        "os.dup2",
        "os.truncate",
        "os.ftruncate",
        "os.replace",
        "os.rename",
        "os.renames",
        "os.remove",
        "os.unlink",
        "os.mkdir",
        "os.makedirs",
        "os.rmdir",
        "os.removedirs",
        "os.chmod",
        "os.chown",
        "os.symlink",
        "os.link",
        "os.utime",
        "os.mkfifo",
        "glob.glob",
        "glob.iglob",
        "pkgutil.get_data",
        "inspect.getsource",
        "inspect.getsourcefile",
        "inspect.getsourcelines",
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
        "lstat",
        "iterdir",
        "rglob",
        "is_symlink",
        "readlink",
        "touch",
        "chmod",
        "lchmod",
        "samefile",
        "symlink_to",
        "hardlink_to",
    }
)
_PATH_AMBIGUOUS_METHODS = frozenset({"replace", "open", "glob", "resolve"})
_PATH_CLASSES = frozenset({"pathlib.Path", "pathlib.PosixPath", "pathlib.PurePath"})
_PATH_METHODS = _PATH_IO_METHODS | _PATH_AMBIGUOUS_METHODS
_PATH_DERIVED = frozenset(
    {
        "resolve",
        "with_name",
        "with_suffix",
        "with_stem",
        "expanduser",
        "absolute",
        "joinpath",
        "relative_to",
    }
)
_PATH_ITERATORS = frozenset({"iterdir", "glob", "rglob"})
_PATH_WRAPPERS = frozenset({"sorted", "list", "tuple", "set", "frozenset", "reversed", "iter"})
_CALL_ALIASES = {
    "datetime.datetime.now": "datetime.now",
    "datetime.datetime.utcnow": "datetime.utcnow",
    "builtins.open": "open",
    "io.open": "open",
}

_IMPURE_IMPORT_HINT = "move effectful imports to an adapter outside the strict core"
_IMPURE_CALL_HINT = (
    "pass observed clock, environment, randomness, or I/O values from their effect owner; "
    "injected callables can still perform effects"
)
_ASYNC_HINT = (
    "async syntax is rejected by this rule; keep orchestration at its service or adapter owner"
)
_UNFROZEN_HINT = "use `@dataclass(frozen=True)` to prevent ordinary field rebinding; it does not freeze nested values"
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
    call_funcs: set[int] = field(default_factory=set[int])
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

    # meiva:note
    # The names a target binds that stand for paths: `for entry in root.iterdir()`, a comprehension
    # over `root.rglob("*.py")`, and a loop over a name annotated as a collection of paths.
    def record_loop(self, target: ast.expr, iterable: ast.expr, dotted: _Dotted) -> None:
        if isinstance(target, ast.Name) and _iterates_paths(iterable, dotted, self.path_vars):
            self.path_vars.add(target.id)

    # meiva:note
    # A name or attribute that denotes an impure call without calling it (`_is_file = Path.is_file`,
    # `map(Path.is_file, xs)`, `partial(os.stat, p)`, a default argument `read=os.stat`, `_open = open`,
    # `stat = root.stat`): the reference is the call under another name, and every use of it would
    # otherwise be a call the scanner does not see. Recorded once, where it is named. Only the code's
    # own list counts: a class the policy lists as a shell constructor is also an annotation.
    def record_reference(
        self,
        node: ast.Name | ast.Attribute,
        resolved: str | None,
        dotted: _Dotted,
        *,
        unshadowed_open: bool = False,
    ) -> None:
        if id(node) in self.call_funcs:
            return
        what = _referenced_call(
            node, resolved, dotted, self.path_vars, unshadowed_open=unshadowed_open
        )
        if what is not None:
            self.items.append(
                PurityItem(
                    node.lineno,
                    Code.IMPURE_CALL,
                    what,
                    f"impure call `{what}` named without a call in strict module",
                    _IMPURE_CALL_HINT,
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


# meiva:note
# A comparison validates the old snapshot before installing the current rule policy. Discard
# root-keyed settings after that mutation so the second scan sees the replacement policy.
def invalidate_policy_cache() -> None:
    _rules_for_root.cache_clear()


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
        if target in {
            "pathlib.Path",
            "pathlib.PosixPath",
            "Path",
            "pathlib.Path.home",
            "pathlib.Path.cwd",
        }:
            return True
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in _PATH_DERIVED:
            return _is_path_expr(expr.func.value, dotted, path_vars)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        return _is_path_expr(expr.left, dotted, path_vars)
    if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Attribute):
        return expr.value.attr == "parents" and _is_path_expr(expr.value.value, dotted, path_vars)
    if isinstance(expr, ast.Attribute):
        if expr.attr == "parent":
            return _is_path_expr(expr.value, dotted, path_vars)
        return isinstance(expr.value, ast.Name) and f"{expr.value.id}.{expr.attr}" in path_vars
    return False


def _method_spelling(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def _iterates_paths(expr: ast.expr, dotted: _Dotted, path_vars: set[str]) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in path_vars
    if not isinstance(expr, ast.Call):
        return False
    if isinstance(expr.func, ast.Attribute) and expr.func.attr in _PATH_ITERATORS:
        return _is_path_expr(expr.func.value, dotted, path_vars)
    spelled = expr.func.id if isinstance(expr.func, ast.Name) else _method_spelling(expr.func)
    if spelled in {*_PATH_WRAPPERS, "dict.fromkeys"} and expr.args:
        return _iterates_paths(expr.args[0], dotted, path_vars)
    return False


def _referenced_call(
    node: ast.Name | ast.Attribute,
    resolved: str | None,
    dotted: _Dotted,
    path_vars: set[str],
    *,
    unshadowed_open: bool,
) -> str | None:
    if unshadowed_open:
        return "open"
    if resolved is not None:
        norm = _CALL_ALIASES.get(resolved, resolved)
        if norm in DEFAULT_IMPURE_CALLS and norm != _OS_ENVIRON:
            return norm
        owner, _, attr = resolved.rpartition(".")
        return f"Path.{attr}" if owner in _PATH_CLASSES and attr in _PATH_METHODS else None
    if (
        isinstance(node, ast.Attribute)
        and node.attr in _PATH_METHODS
        and _is_path_expr(node.value, dotted, path_vars)
    ):
        return f"Path.{node.attr}"
    return None


def _bind_path_names(
    state: PurityState,
    target: ast.expr,
    value: ast.expr | None,
    annotation: ast.expr | None,
    dotted: _Dotted,
) -> None:
    if isinstance(target, ast.Name):
        key = target.id
    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        key = f"{target.value.id}.{target.attr}"
    else:
        return
    from_annotation = annotation is not None and _is_path_annotation(annotation)
    if from_annotation or (value is not None and _is_path_expr(value, dotted, state.path_vars)):
        state.path_vars.add(key)
        if from_annotation and isinstance(target, ast.Name):
            state.path_vars.add(f"self.{key}")


def record_assign(state: PurityState, node: ast.Assign | ast.AnnAssign, dotted: _Dotted) -> None:
    annotation = node.annotation if isinstance(node, ast.AnnAssign) else None
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) == 1:
        _bind_path_names(state, targets[0], node.value, annotation, dotted)


def record_call(
    state: PurityState,
    node: ast.Call,
    callee: str | None,
    dotted: _Dotted,
    *,
    unshadowed_open: bool,
) -> None:
    state.call_funcs.add(id(node.func))
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
