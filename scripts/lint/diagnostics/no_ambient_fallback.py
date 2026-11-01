# meiva:note
# Anti-ambient-fallback AST check (code-placement-and-purity.md#L55):
# "Never add an ambient fallback in the callee (env: Mapping | None = None that falls back to
# os.environ, or now: datetime | None = None that calls datetime.now()). Defaults on parameters
# are reserved for pure constants and tuning numbers."

from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402
from meizel.checkout import roles_at  # noqa: E402
from meizel.roles import select_packages  # noqa: E402

ROLES = roles_at(_REPO_ROOT)
_SCAN_DIRS = select_packages(ROLES, "application", "standalone", "templates")

__all__ = ["check_file", "main", "violations"]

_DIAGNOSTIC_TEMPLATE = (
    "ambient fallback for parameter '{param}' to {ambient} is forbidden — "
    "callee must not provide ambient fallbacks "
    "(docs/conventions/architecture/code-placement-and-purity.md#L55). "
    "Defaults on parameters are reserved for pure constants and tuning numbers. "
    "Pass the value explicitly from the composition root instead."
)


def _is_none(expr: ast.expr) -> bool:
    return isinstance(expr, ast.Constant) and expr.value is None


# meiva:note
# Map parameter name to definition line for parameters with None default.
def _none_parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, int]:
    result: dict[str, int] = {}
    args = func.args
    pos_args = args.posonlyargs + args.args
    if args.defaults:
        non_default_count = len(pos_args) - len(args.defaults)
        for i, default in enumerate(args.defaults):
            if _is_none(default):
                arg = pos_args[non_default_count + i]
                result[arg.arg] = arg.lineno
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=False):
        if default is not None and _is_none(default):
            result[arg.arg] = arg.lineno
    return result


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value_name = _dotted_name(node.value)
        if value_name is not None:
            return f"{value_name}.{node.attr}"
    return None


def _ambient_call_source(node: ast.Call) -> str | None:
    name = _dotted_name(node.func)
    if name in ("datetime.now", "datetime.datetime.now", "now"):
        return "datetime.now()"
    if name in ("datetime.utcnow", "datetime.datetime.utcnow", "utcnow"):
        return "datetime.utcnow()"
    if name in ("time.time",):
        return "time.time()"
    if name in ("os.environ.get", "environ.get", "os.environ.copy", "environ.copy"):
        return "os.environ"
    return None


def _ambient_source(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _ambient_call_source(node)
    if isinstance(node, ast.Subscript):
        name = _dotted_name(node.value)
        return "os.environ" if name in ("os.environ", "environ") else None
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _dotted_name(node)
        return "os.environ" if name in ("os.environ", "environ") else None
    return None


def _find_ambient(node: ast.AST) -> str | None:
    direct = _ambient_source(node)
    if direct is not None:
        return direct
    for child in ast.walk(node):
        found = _ambient_source(child)
        if found is not None:
            return found
    return None


# meiva:note
# Return (param_name, is_none_branch_true) if test checks whether param is None/falsy.
def _none_test(test: ast.expr) -> tuple[str, bool] | None:
    if isinstance(test, ast.Name):
        return test.id, False
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
    ):
        return test.operand.id, True
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op = test.ops[0]
        left, right = test.left, test.comparators[0]
        name = (
            left.id
            if isinstance(left, ast.Name) and _is_none(right)
            else (right.id if isinstance(right, ast.Name) and _is_none(left) else None)
        )
        if name is not None:
            if isinstance(op, (ast.Is, ast.Eq)):
                return name, True
            if isinstance(op, (ast.IsNot, ast.NotEq)):
                return name, False
    return None


def _targets_of(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    return list(node.targets) if isinstance(node, ast.Assign) else [node.target]


def _assigns_to(target: ast.expr, name: str) -> bool:
    if isinstance(target, ast.Name):
        return target.id == name
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
        return target.value.id == "self" and target.attr == name
    return False


def _fallback_target(target: ast.expr, none_params: dict[str, int]) -> str | None:
    for param in none_params:
        if _assigns_to(target, param):
            return param
    return None


# meiva:note
# Walk nodes of node without descending into inner functions or classes.
def _own_nodes(node: ast.AST) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes.append(child)
        nodes.extend(_own_nodes(child))
    return nodes


def _check_or_fallback(expr: ast.expr, param: str) -> str | None:
    if not (isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or) and expr.values):
        return None
    first = expr.values[0]
    if isinstance(first, ast.Name) and first.id == param:
        for val in expr.values[1:]:
            amb = _find_ambient(val)
            if amb is not None:
                return amb
    return None


def _check_ifexp_fallback(expr: ast.expr, param: str) -> str | None:
    if not isinstance(expr, ast.IfExp):
        return None
    tested = _none_test(expr.test)
    if tested is not None and tested[0] == param:
        fb = expr.body if tested[1] else expr.orelse
        return _find_ambient(fb)
    return None


def _check_assign_value(value: ast.expr, param: str) -> str | None:
    return _check_or_fallback(value, param) or _check_ifexp_fallback(value, param)


def _check_assign(
    node: ast.Assign | ast.AnnAssign, none_params: dict[str, int]
) -> list[tuple[int, str]]:
    if node.value is None:
        return []
    out: list[tuple[int, str]] = []
    for target in _targets_of(node):
        param = _fallback_target(target, none_params)
        if param is not None:
            amb = _check_assign_value(node.value, param)
            if amb is not None:
                out.append((node.lineno, _DIAGNOSTIC_TEMPLATE.format(param=param, ambient=amb)))
    return out


def _check_return(node: ast.Return, none_params: dict[str, int]) -> list[tuple[int, str]]:
    if node.value is None:
        return []
    out: list[tuple[int, str]] = []
    for param in none_params:
        amb = _check_assign_value(node.value, param)
        if amb is not None:
            out.append((node.lineno, _DIAGNOSTIC_TEMPLATE.format(param=param, ambient=amb)))
    return out


def _check_if_body(branch: list[ast.stmt], param: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for stmt in branch:
        for inner in ast.walk(stmt):
            if not isinstance(inner, (ast.Assign, ast.AnnAssign)) or inner.value is None:
                continue
            if any(_assigns_to(target, param) for target in _targets_of(inner)):
                amb = _find_ambient(inner.value)
                if amb is not None:
                    out.append(
                        (inner.lineno, _DIAGNOSTIC_TEMPLATE.format(param=param, ambient=amb))
                    )
    return out


def _check_if(node: ast.If, none_params: dict[str, int]) -> list[tuple[int, str]]:
    tested = _none_test(node.test)
    if tested is None or tested[0] not in none_params:
        return []
    param, is_none_true = tested
    branch = node.body if is_none_true else node.orelse
    return _check_if_body(branch, param)


def _function_violations(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    none_params = _none_parameters(func)
    if not none_params:
        return []
    raw: list[tuple[int, str]] = []
    for node in _own_nodes(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            raw.extend(_check_assign(node, none_params))
        elif isinstance(node, ast.Return):
            raw.extend(_check_return(node, none_params))
        elif isinstance(node, ast.If):
            raw.extend(_check_if(node, none_params))
    seen: set[tuple[int, str]] = set()
    out: list[tuple[int, str]] = []
    for line, msg in raw:
        if (line, msg) not in seen:
            seen.add((line, msg))
            out.append((line, msg))
    return out


# meiva:note
# Scan tree for ambient fallback violations, returning [(lineno, message), ...].
def violations(tree: ast.Module, rel_path: str = "") -> list[tuple[int, str]]:
    if rel_path and lint_common.is_test_path(rel_path):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_function_violations(node))
    return sorted(out, key=lambda x: x[0])


# meiva:note
# Return ambient fallback violations for path, relative to repo_root.
def check_file(path: Path, repo_root: Path) -> list[tuple[int, str]]:
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        rel = path.as_posix()
    if lint_common.is_test_path(rel):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError:
        return []
    return violations(tree, rel)


def _resolve_targets(argv: list[str], root: Path) -> list[Path] | None:
    argv, only = lint_common.split_only(argv)
    if only == []:
        return []
    if only is not None:
        scope = lint_common.changed_scope(only, root)
        if scope is None:
            return [root / d for d in _SCAN_DIRS]
        return [root / rel for rel in scope if (root / rel).is_file() and rel.endswith(".py")]
    if argv:
        return [Path(arg).resolve() for arg in argv]
    return [root / d for d in _SCAN_DIRS]


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root.resolve()
    raw_argv = sys.argv[1:] if argv is None else argv
    targets = _resolve_targets(raw_argv, root)
    if targets == []:
        return 0
    files = lint_common.python_files(targets or [], root)
    all_violations: list[tuple[str, int, str]] = [
        (
            path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix(),
            line,
            msg,
        )
        for path in sorted(files)
        for line, msg in check_file(path, root)
    ]
    for rel, line, message in all_violations:
        print(f"{rel}:{line}: {message}")
    if all_violations:
        print(
            f"\n{len(all_violations)} ambient fallback violation(s). Never add an ambient fallback "
            f"in the callee (e.g. now = datetime.now(), env = os.environ). Defaults on parameters "
            f"are reserved for pure constants and tuning numbers. Pass the value explicitly from "
            f"the composition root instead. See docs/conventions/architecture/code-placement-and-purity.md#L55.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
