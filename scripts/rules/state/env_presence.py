# meiva:note
# Rule: whether an environment variable is set is judged by its value, never by its presence.
#
# An optional secret exported empty (`X=` in an env file, an unset CI secret that the runner still
# defines, a `.env` line left blank) is present and means "not configured". A presence test reads it
# as configured and goes on to use an empty credential, which fails later with a message about the
# credential rather than about the configuration. The value test treats empty and absent alike:
# `os.environ.get("X")` is falsy for both, and so is `[ -n "${X:-}" ]` in a shell.
#
# The rule judges every variable, not a list of secret names: a presence test is the wrong shape for
# any optional setting, and a list of names would miss the next secret. Its exemptions are the files
# under `[env-presence] files` in `meizel.policy.toml`, each with its reason.
# meiva:link scripts/rules/docs/rules.md
from __future__ import annotations

import ast
import re

__all__ = ["RULE", "python_findings", "shell_findings"]

RULE = "env-presence"
_PYTHON_FIX = (
    "judge an environment variable by its value: `os.environ.get(NAME)` is falsy when the "
    "variable is absent and when it is empty"
)
_SHELL_FIX = (
    'judge the variable by its value: `[ -n "${NAME:-}" ]` is false when it is unset or empty'
)
# meiva:note
# `[ -v NAME ]` and `[[ -v NAME ]]`: bash's set-or-not test, true for a variable exported empty.
_SHELL_PRESENCE = re.compile(r"\[\[?\s+-v\s+([A-Za-z_][A-Za-z0-9_]*)\s")


# meiva:note
# Whether `node` names the process environment: `os.environ`, or a bare `environ` imported from `os`.
def _is_environ(node: ast.AST, *, bare_environ: bool) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    return bare_environ and isinstance(node, ast.Name) and node.id == "environ"


# meiva:note
# Whether `node` reads one variable by a call that answers None when it is absent:
# `os.environ.get(NAME)` or `os.getenv(NAME)`.
def _is_optional_read(node: ast.AST, *, bare_environ: bool) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    func = node.func
    if func.attr == "get" and _is_environ(func.value, bare_environ=bare_environ):
        return True
    return func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os"


def _imports_bare_environ(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "os"
        and any(alias.name == "environ" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )


def _catches_key_error(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(name, ast.Name) and name.id == "KeyError" for name in names)


# meiva:note
# `os.environ[NAME]` subscripts inside a `try` whose handlers catch `KeyError`: the absence is the
# branch the code takes, so the read is a presence test.
def _guarded_subscripts(tree: ast.AST, *, bare_environ: bool) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not any(map(_catches_key_error, node.handlers)):
            continue
        for statement in node.body:
            lines.extend(
                inner.lineno
                for inner in ast.walk(statement)
                if isinstance(inner, ast.Subscript)
                and isinstance(inner.ctx, ast.Load)
                and _is_environ(inner.value, bare_environ=bare_environ)
            )
    return lines


def _presence_comparisons(tree: ast.AST, *, bare_environ: bool) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        op, right = node.ops[0], node.comparators[0]
        if (
            isinstance(op, ast.In | ast.NotIn) and _is_environ(right, bare_environ=bare_environ)
        ) or (
            isinstance(op, ast.Is | ast.IsNot)
            and isinstance(right, ast.Constant)
            and right.value is None
            and _is_optional_read(node.left, bare_environ=bare_environ)
        ):
            lines.append(node.lineno)
    return lines


# meiva:note
# `(line, message)` for every presence test of an environment variable in one parsed module: `in` /
# `not in os.environ`, `os.environ.get(...)` or `os.getenv(...)` compared with `is None` /
# `is not None`, and `os.environ[...]` read under a `KeyError` handler.
def python_findings(tree: ast.AST) -> list[tuple[int, str]]:
    bare = _imports_bare_environ(tree)
    lines = sorted(
        {
            *_presence_comparisons(tree, bare_environ=bare),
            *_guarded_subscripts(tree, bare_environ=bare),
        }
    )
    return [(line, f"environment presence test: {_PYTHON_FIX}") for line in lines]


# meiva:note
# `(line, message)` for every `[ -v NAME ]` in a shell text: a script, or the `run:` blocks a
# workflow file holds as text.
def shell_findings(text: str) -> list[tuple[int, str]]:
    return [
        (number, f"`-v {match.group(1)}` is a presence test: {_SHELL_FIX}")
        for number, line in enumerate(text.splitlines(), 1)
        for match in _SHELL_PRESENCE.finditer(line)
    ]
