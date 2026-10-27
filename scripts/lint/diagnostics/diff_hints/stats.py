# meiva:note
# Shapes that are counted and never asked. Each is a candidate for a hint: the count over a change, or
# over the whole tree, says how often it occurs and where before anyone is asked to answer for it.
#
#   optional-test-only   a parameter with a default that a change adds to a production function, and
#                        that only tests pass by keyword (the shell checks the callers)
#   optional-fallback    a parameter typed `X | None` without a default that falls back to a real
#                        implementation when it is None
#   clock-in-callee      a `Clock(...)` or `Clock.from_settings()` built inside a function that takes
#                        parameters, in place of taking the clock
from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence

from scripts.lint.diagnostics.diff_hints.model import (
    FileChange,
    FileKind,
    Importers,
    Roles,
    StatId,
    StatSite,
    Unit,
)
from scripts.lint.diagnostics.diff_hints.names import dotted
from scripts.lint.diagnostics.diff_hints.scope import parse_unit
from scripts.lint.diagnostics.diff_hints.signatures import optional_fallback
from scripts.lint.diagnostics.diff_hints.walk import own_nodes, parameters

_CLOCKS = frozenset({"base.clock.Clock", "base.clock.Clock.from_settings"})
_RECEIVERS = frozenset({"self", "cls"})


# meiva:note
# The parameters with a default that the change adds: candidates for `optional-test-only`.
def optional_parameters(unit: Unit) -> list[StatSite]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    return [
        StatSite(StatId.OPTIONAL_TEST_ONLY, unit.path, arg.lineno, f"{function.name}:{arg.arg}")
        for function in unit.functions
        for arg, default in parameters(function)
        if default is not None and arg.lineno in unit.added and arg.arg not in _RECEIVERS
    ]


def clock_in_callee(unit: Unit) -> list[StatSite]:
    if unit.kind is not FileKind.PRODUCTION:
        return []
    sites: list[StatSite] = []
    for function in unit.functions:
        takes = [arg for arg, _ in parameters(function) if arg.arg not in _RECEIVERS]
        if not takes:
            continue
        for node in own_nodes(function):
            if (
                isinstance(node, ast.Call)
                and node.lineno in unit.added
                and dotted(node.func, unit.names) in _CLOCKS
            ):
                detail = f"`{function.name}` takes parameters and builds a clock itself"
                sites.append(StatSite(StatId.CLOCK_IN_CALLEE, unit.path, node.lineno, detail))
    return sites


# meiva:note
# Every statistic site of a change, with the `optional-test-only` candidates still unfiltered: the
# shell keeps those that only tests pass.
def statistics(
    changes: Sequence[FileChange], texts: Mapping[str, str], roles: Roles
) -> list[StatSite]:
    sites: list[StatSite] = []
    for change in changes:
        text = texts.get(change.path)
        unit = (
            parse_unit(change.path, text, change.added, roles, frozenset(), Importers((), ()))
            if text is not None
            else None
        )
        if unit is not None:
            sites.extend(
                [*optional_parameters(unit), *optional_fallback(unit), *clock_in_callee(unit)]
            )
    return sorted(sites, key=lambda site: (site.stat.value, site.path, site.line))
