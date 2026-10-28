from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from base.events.contract import EVENTS

pytestmark = pytest.mark.registry
_REPO = Path(__file__).resolve().parents[3]
_CALLEES = {"emit", "emit_prepared", "prepare_event"}
_CATEGORIES = {"audit", "telemetry", "log"}

# meiva:note
# `schedules.jobs.telemetry_event.emit_system_event(name, attributes)` is a wrapper whose own call
# into `telemetry.emit` passes a variable, so the scan above cannot see the name it emits. That is
# exactly how `agent_watch_round` shipped undeclared: `emit()` raises for it every round, and the
# round's own handler turned the raise into one line of text. A wrapper is a call shape the scan
# has to know, not a place where the rule stops applying.
_WRAPPER = "emit_system_event"
_WRAPPER_CATEGORY = "telemetry"


def _production_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=_REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [
        _REPO / name
        for name in tracked
        if "/tests/" not in name and not name.startswith(("tests/", "scripts/", "meizel/"))
    ]


def _literal(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


# meiva:note
# A module-level string constant is how a module names the event it emits once and uses twice:
# `EVENT_NAME = "agent_watch_round"` then `emit_system_event(EVENT_NAME, ...)`. Reading only
# literals at the call site stops at the constant, which is the second half of how that name
# shipped undeclared. A name rebound anywhere in the module is not a constant and stays unresolved.
def _module_strings(tree: ast.Module) -> dict[str, str]:
    values: dict[str, str] = {}
    rebound: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            literal = _literal(value)
            if literal is None or target.id in values or target.id in rebound:
                rebound.add(target.id)
            else:
                values[target.id] = literal
    return {name: value for name, value in values.items() if name not in rebound}


def _named(node: ast.expr | None, constants: dict[str, str]) -> str | None:
    literal = _literal(node)
    if literal is not None:
        return literal
    return constants.get(node.id) if isinstance(node, ast.Name) else None


def literal_emit_sites(source: str) -> list[tuple[int, str, str]]:
    sites: list[tuple[int, str, str]] = []
    tree = ast.parse(source)
    constants = _module_strings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee == _WRAPPER and node.args:
            name = _named(node.args[0], constants)
            if name is not None:
                sites.append((node.lineno, _WRAPPER_CATEGORY, name))
            continue
        if callee not in _CALLEES or len(node.args) < 2:
            continue
        category, name = _literal(node.args[0]), _named(node.args[1], constants)
        if category in _CATEGORIES and name is not None:
            sites.append((node.lineno, category, name))
    return sites


def test_the_scan_recognises_the_call_shapes_in_use() -> None:
    source = (
        "from base import telemetry\n"
        "from base.telemetry import emit\n"
        "telemetry.emit('telemetry', 'a')\n"
        "emit('audit', 'b', attributes={})\n"
        "telemetry.emit(\n    'telemetry',\n    'c',\n    attributes={},\n)\n"
        "telemetry.emit('telemetry', name)\n"
        "other.emit('not-a-category', 'd')\n"
        "emit_system_event('e', {}, process='p')\n"
        "emit_system_event(name, {})\n"
        "EVENT_NAME = 'f'\n"
        "emit_system_event(EVENT_NAME, {})\n"
        "REBOUND = 'g'\n"
        "REBOUND = name\n"
        "emit_system_event(REBOUND, {})\n"
    )
    assert literal_emit_sites(source) == [
        (3, "telemetry", "a"),
        (4, "audit", "b"),
        (5, "telemetry", "c"),
        (12, "telemetry", "e"),
        (15, "telemetry", "f"),
    ]


def test_every_literal_emit_site_names_a_declared_event_in_a_declared_category() -> None:
    undeclared: list[str] = []
    mismatched: list[str] = []
    scanned = 0
    for path in _production_files():
        for line, category, name in literal_emit_sites(path.read_text(encoding="utf-8")):
            scanned += 1
            where = f"{path.relative_to(_REPO)}:{line}: {category}/{name}"
            spec = EVENTS.get(name)
            if spec is None:
                undeclared.append(where)
            elif category != spec.category and category not in spec.extra_categories:
                mismatched.append(f"{where} (declared {spec.category})")
    assert scanned > 30, "the scan found almost no call sites: it is looking in the wrong place"
    assert undeclared == [], (
        "emit() raises for these event names: declare them in base/events/declarations"
    )
    assert mismatched == [], "emit() raises for these categories: they contradict the declaration"
