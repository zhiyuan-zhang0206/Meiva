# meiva:note
# Hints about reading the world instead of taking a value: the clock, the environment,
# the platform, randomness and the whole `Settings` object (`ambient-read`), and the
# service-locator shapes that fetch a component's inputs from the application
# (`service-locator`). Both look at production files only and skip a composition root
# by manifest kind; the existing filename heuristic remains for legacy roots.
from __future__ import annotations

import ast

from meizel.graph.model import INTERVAL_CLOCKS, AmbientKind, stdlib_kind
from scripts.lint.diagnostics.diff_hints.model import FileKind, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.names import OS_ENVIRON, OS_GETENV, dotted
from scripts.lint.diagnostics.diff_hints.scope import is_root_file
from scripts.lint.diagnostics.diff_hints.walk import own_nodes

_AMBIENT_NAMES = frozenset(
    {
        "time.time",
        "time.time_ns",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        OS_GETENV,
        OS_ENVIRON,
        "os.getcwd",
        "os.getcwdb",
        "pathlib.Path.home",
        "pathlib.Path.cwd",
        "socket.gethostname",
        "socket.getfqdn",
        "sys.platform",
    }
)
_AMBIENT_MODULES = ("platform.", "random.")
_DETERMINISTIC = frozenset({"random.Random"})
_SETTINGS = "base.config.settings"
_LOCATOR_CALLS = frozenset({"runtime_view", "settings_view"})
_HANDOVER_NAMES = frozenset({"now", "at", "ts", "timestamp"})
_HANDOVER_SUFFIXES = ("_at", "_ts", "_now", "_time")


def _is_ambient(name: str) -> bool:
    if name in _DETERMINISTIC:
        return False
    return name in _AMBIENT_NAMES or name in INTERVAL_CLOCKS or name.startswith(_AMBIENT_MODULES)


def _read_name(node: ast.AST, unit: Unit) -> str | None:
    if isinstance(node, ast.Attribute):
        name = dotted(node, unit.names)
        if name is not None and _is_ambient(name):
            return ast.unparse(node)
        if dotted(node.value, unit.names) == _SETTINGS:
            return ast.unparse(node)
    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        name = dotted(node, unit.names)
        if name is not None and _is_ambient(name):
            return node.id
    return None


# meiva:note
# The clock reads that are handed straight to a call as its `now` (or `*_at`, `*_ts`): the
# caller reads once and passes the value, which is the form the hint asks for.
def _handed_over(unit: Unit) -> frozenset[int]:
    handed: set[int] = set()
    for call in unit.calls:
        for keyword in call.keywords:
            name = keyword.arg or ""
            named = name in _HANDOVER_NAMES or name.endswith(_HANDOVER_SUFFIXES)
            if named and isinstance(keyword.value, ast.Call):
                handed.add(id(keyword.value.func))
    return frozenset(handed)


def _is_sleep(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    callee = node.func
    return (isinstance(callee, ast.Name) and callee.id == "sleep") or (
        isinstance(callee, ast.Attribute) and callee.attr == "sleep"
    )


# meiva:note
# The nodes that sit in a function which also sleeps (in its own body, not a nested def).
def _in_waiting_function(unit: Unit) -> frozenset[int]:
    waiting: set[int] = set()
    for function in unit.functions:
        nodes = list(own_nodes(function))
        if any(_is_sleep(node) for node in nodes):
            waiting.update(id(node) for node in nodes)
    return frozenset(waiting)


def _is_interval_read(node: ast.AST, unit: Unit) -> bool:
    target = node
    if not isinstance(target, ast.Attribute | ast.Name):
        return False
    return dotted(target, unit.names) in INTERVAL_CLOCKS


def ambient_read(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION or is_root_file(unit.path):
        return []
    sites: list[Site] = []
    handed = _handed_over(unit)
    waiting = _in_waiting_function(unit)
    for node in unit.touched:
        if id(node) in handed or (_is_interval_read(node, unit) and id(node) not in waiting):
            continue
        read = _read_name(node, unit)
        target = dotted(node, unit.names)
        acquired = None if target is None else stdlib_kind(target)
        kind = AmbientKind.SETTINGS if acquired is None else acquired[0]
        if read is not None and kind not in unit.roots:
            line = getattr(node, "lineno", 0)
            detail = f"`{read}` read in a non-root module"
            sites.append(Site(HintId.AMBIENT_READ, unit.path, line, detail))
    return sites


def _locator_detail(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and node.attr == "state":
        holder = node.value
        is_app = isinstance(holder, ast.Name) and holder.id == "app"
        if is_app or (isinstance(holder, ast.Attribute) and holder.attr == "app"):
            return f"`{ast.unparse(node)}` fetches inputs from the application"
    if isinstance(node, ast.Call):
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", "")
        if name in _LOCATOR_CALLS:
            return f"`{name}(...)` fetches the whole configuration"
    return None


def service_locator(unit: Unit) -> list[Site]:
    if (
        unit.kind is not FileKind.PRODUCTION
        or is_root_file(unit.path)
        or AmbientKind.SETTINGS in unit.roots
    ):
        return []
    sites: list[Site] = []
    for node in unit.touched:
        detail = _locator_detail(node)
        if detail is not None:
            sites.append(
                Site(HintId.SERVICE_LOCATOR, unit.path, getattr(node, "lineno", 0), detail)
            )
    return sites
