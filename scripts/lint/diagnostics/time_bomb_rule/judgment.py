# meiva:note
# The three rules and their messages: rule 1 judges source files, rules 2 and 3
# judge test files. The wording of every finding is part of the contract other
# suites quote, so a sentence change here is a change of contract.
# meiva:link scripts/lint/docs/time-bomb-rule-parts.meiva.okf.md

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from meizel import lint_common
from scripts.lint.diagnostics.time_bomb_rule.index import Index
from scripts.lint.diagnostics.time_bomb_rule.sources import (
    CLOCK_PARAMS,
    HTTP_METHODS,
    HTTP_NAMES,
    OPT_OUT,
    WINDOW_NAMES,
    is_calendar_literal,
    is_real_now_call,
)

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)


def _is_test_path(rel: str) -> bool:
    return any(pat.search(rel) for pat in _TEST_PATTERNS)


def _rel_or_abs(root: Path, path: Path) -> str:
    return path.as_posix().removeprefix(root.as_posix() + "/")


def lint_source(index: Index, paths: list[Path], scope: frozenset[str] | None = None) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in lint_common.python_files([path], index.root):
                rel = _rel_or_abs(index.root, p)
                if _is_test_path(rel) or (scope is not None and rel not in scope):
                    continue
                errors.extend(lint_source_file(index, p, rel))
        elif path.suffix == ".py":
            rel = _rel_or_abs(index.root, path)
            if not _is_test_path(rel):
                errors.extend(lint_source_file(index, path, rel))
    return errors


def _unthreaded_calls(
    index: Index, mod: str, fn_node: ast.AST, clock: frozenset[str]
) -> Iterator[tuple[ast.Call, tuple[str, str]]]:
    for sub in ast.walk(fn_node):
        if not isinstance(sub, ast.Call):
            continue
        resolved = index._resolve(mod, sub)
        if resolved is None:
            continue
        cuses, ccovered, cfam = index.summary(*resolved)
        if not (cuses and cfam):
            continue
        if index._inside_fallback(fn_node, sub, clock):
            continue
        if ccovered and index._call_threaded(sub, resolved, clock):
            continue
        yield sub, resolved


def lint_source_file(index: Index, path: Path, rel: str) -> list[str]:
    mod = rel[:-3].replace("/", ".")
    errors: list[str] = []
    fns = index.functions(mod)
    for name, (fn_node, params) in fns.items():
        clock = params & CLOCK_PARAMS
        if not clock:
            continue
        uses, covered, family = index.summary(mod, name)
        if not (uses and not covered and family):
            continue
        seen: set[tuple[str, str]] = set()
        for sub, (cmod, cname) in _unthreaded_calls(index, mod, fn_node, clock):
            if (cmod, cname) in seen:
                continue
            seen.add((cmod, cname))
            errors.append(
                f"{path}:{sub.lineno}: {name} accepts a clock parameter "
                f"({', '.join(sorted(clock))}) but calls {cmod}.{cname} — which "
                "uses the real clock against a fixed-instant boundary — without "
                "threading it; pass the clock through (now=.../at=...) so tests "
                "can pin the window instead of riding the wall clock"
            )
    return errors


def lint_tests(index: Index, paths: list[Path], scope: frozenset[str] | None = None) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            for p in lint_common.python_files([path], index.root):
                rel = _rel_or_abs(index.root, p)
                if _is_test_path(rel) and (scope is None or rel in scope):
                    errors.extend(lint_test_file(index, p, rel))
        elif path.suffix == ".py":
            rel = _rel_or_abs(index.root, path)
            if _is_test_path(rel):
                errors.extend(lint_test_file(index, path, rel))
    return errors


def _fixed_imports(index: Index, nodes: Iterable[ast.AST]) -> tuple[set[str], dict[str, str]]:
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            cand = node.module
            if cand in index.fixed:
                for alias in node.names:
                    if alias.name in index.fixed[cand]:
                        names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                leaf = alias.name.split(".")[-1]
                if alias.name in index.fixed:
                    aliases[alias.asname or leaf] = alias.name
    return names, aliases


def _local_derives(
    function: ast.AST, fixed_names: set[str], aliases: dict[str, str], index: Index
) -> tuple[set[str], set[str]]:
    fixed: set[str] = set()
    real: set[str] = set()

    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                if isinstance(target, ast.Name):
                    if any(is_real_now_call(n) for n in ast.walk(sub.value)):
                        real.add(target.id)
                        fixed.discard(target.id)
                    elif _expr_refs_fixed(sub.value, fixed_names, fixed, aliases, index):
                        fixed.add(target.id)
                        real.discard(target.id)
    return fixed, real


def _expr_refs_fixed(
    expr: ast.AST,
    fixed_names: set[str],
    local_fixed: set[str],
    aliases: dict[str, str],
    index: Index,
) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id in (fixed_names | local_fixed):
            return True
        if (
            isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in aliases
            and n.attr in index.fixed_names(aliases[n.value.id])
        ):
            return True
    return False


def _opaque_or_real_now(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name) and f.id == "TestClient":
        return "TestClient (opaque HTTP)"
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id in HTTP_NAMES and f.attr in HTTP_METHODS:
            return f"{f.value.id}.{f.attr} (opaque HTTP)"
        if is_real_now_call(call):
            return f"{f.value.id}.{f.attr}"
    return None


def _tainted(function: ast.AST, index: Index, mod: str) -> list[str]:
    taints: list[str] = []
    for sub in ast.walk(function):
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub is not function:
            continue
        if not isinstance(sub, ast.Call):
            continue
        opaque = _opaque_or_real_now(sub)
        if opaque is not None:
            taints.append(opaque)
            continue
        resolved = index._resolve(mod, sub)
        if resolved is None:
            continue
        cuses, ccovered, _cfam = index.summary(*resolved)
        if not cuses:
            continue
        if ccovered and index._call_threaded(sub, resolved, frozenset()):
            continue
        taints.append(f"{resolved[0]}.{resolved[1]} (clock not pinned)")
    return taints


def _node_bindings(node: ast.AST) -> Iterator[tuple[str, int, int | None, ast.AST]]:
    if isinstance(node, ast.Dict):
        yield from _dict_bindings(node)
    elif isinstance(node, ast.keyword):
        if node.arg in WINDOW_NAMES and is_calendar_literal(node.value):
            yield node.arg, node.value.lineno, node.value.end_lineno, node.value
    elif (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in WINDOW_NAMES
        and is_calendar_literal(node.value)
    ):
        yield node.targets[0].id, node.targets[0].lineno, node.value.end_lineno, node.value


def _dict_bindings(node: ast.Dict) -> Iterator[tuple[str, int, int | None, ast.AST]]:
    for key, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value in WINDOW_NAMES
            and is_calendar_literal(value)
        ):
            yield key.value, key.lineno, value.end_lineno, value


def _lint_fixture_dates(path: Path, nodes: Iterable[ast.AST], lines: list[str]) -> list[str]:
    found: list[tuple[int, str]] = []

    def check(name: str, start: int, end: int | None, value: ast.AST) -> None:
        stop = end if end is not None else start
        if any(OPT_OUT in line for line in lines[start - 1 : stop]):
            return
        found.append(
            (
                start,
                f"{path}:{start}: time-bomb fixture date: a fixed calendar "
                f"literal ({ast.unparse(value)}) bound to the window-shaped "
                f"name '{name}'; derive it from the clock or add "
                f"'# {OPT_OUT}: <reason>' to opt out",
            )
        )

    for node in nodes:
        for name, start, end, value in _node_bindings(node):
            check(name, start, end, value)

    return [message for _, message in sorted(found)]


def _equality_errors(
    path: Path,
    function: ast.AST,
    source_lines: list[str],
    taints: list[str],
    refs_fixed: Callable[[ast.AST], bool],
) -> list[str]:
    errors: list[str] = []
    for sub in ast.walk(function):
        if not (
            isinstance(sub, ast.Compare)
            and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops)
        ):
            continue
        if not any(refs_fixed(side) for side in [sub.left, *sub.comparators]):
            continue
        if any(OPT_OUT in line for line in source_lines[sub.lineno - 1 : sub.end_lineno]):
            continue
        errors.append(
            f"{path}:{sub.lineno}: time-bomb test: exact equality "
            "on a value derived from a fixed instant while the "
            f"derivation can reach the real clock ({'; '.join(taints)}); "
            "pin the clock (pass now=...), assert with a tolerance, or "
            f"add '# {OPT_OUT}: <reason>' to opt out"
        )
    return errors


def lint_test_file(index: Index, path: Path, rel: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    source_lines = text.splitlines()
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.ImportFrom, ast.Import, ast.Dict, ast.keyword, ast.Assign))
    ]
    errors: list[str] = _lint_fixture_dates(path, nodes, source_lines)
    mod = rel[:-3].replace("/", ".")
    fixed_names, aliases = _fixed_imports(index, nodes)
    if not fixed_names and not aliases:
        return errors
    for node in tree.body:
        if not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not node.name.startswith("test_"):
            continue
        fixed_local, _real_local = _local_derives(node, fixed_names, aliases, index)
        taints = _tainted(node, index, mod)
        if not taints:
            continue
        errors.extend(
            _equality_errors(
                path,
                node,
                source_lines,
                taints,
                lambda e, fl=fixed_local: _expr_refs_fixed(e, fixed_names, fl, aliases, index),
            )
        )
    return errors
