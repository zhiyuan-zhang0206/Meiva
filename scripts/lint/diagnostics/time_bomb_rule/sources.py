# meiva:note
# The vocabulary both halves of the rule share: which parameter names carry a
# caller-visible clock, which calls read the real clock, which names are
# window-shaped, and what counts as a fixed calendar literal. Why each name
# is in (or deliberately out of) a set is in the node below.
# meiva:link scripts/lint/docs/time-bomb-rule-parts.meiva.okf.md

from __future__ import annotations

import ast
import re

CLOCK_PARAMS = frozenset({"now", "now_utc", "at", "as_of", "clock", "when", "timestamp", "instant"})

REAL_NOW_ATTRS = frozenset({"now", "utcnow", "today"})


def is_real_now_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in ("datetime", "time")
        and node.func.attr in REAL_NOW_ATTRS
    )


HTTP_NAMES = frozenset({"client", "session", "httpx", "requests"})
HTTP_METHODS = frozenset({"get", "post", "put", "delete", "request", "patch"})
OPT_OUT = "time-bomb-ok"

WINDOW_NAMES = frozenset({"day", "date", "since", "until", "window_start", "window_end"})
CALENDAR_STR = re.compile(r"^\d{4}-\d{2}-\d{2}($|[T ])")


def is_calendar_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(CALENDAR_STR.match(node.value))
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return (
            name in ("date", "datetime")
            and len(node.args) >= 3
            and all(isinstance(arg, ast.Constant) for arg in node.args)
        )
    return False
