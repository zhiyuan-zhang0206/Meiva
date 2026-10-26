# meiva:note
# The service-locator shapes that fetch a component's inputs from the application.
# Ambient acquisitions belong to meizel check. This hint recognizes settings roots
# from the same manifest revision as the source, with the legacy filename heuristic
# retained for existing process entries. The same declarations tell a root from a middle layer.
from __future__ import annotations

import ast

from meizel.graph.model import AmbientKind
from scripts.lint.diagnostics.diff_hints.model import FileKind, HintId, Site, Unit
from scripts.lint.diagnostics.diff_hints.scope import is_root_file

_LOCATOR_CALLS = frozenset({"runtime_view", "settings_view"})


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


MIN_IMPORTERS = 3


# meiva:note
# A file declared under `[roots]` that many modules import is a middle layer, not a composition
# root: every importer reads the world through it, and a test cannot vary what it reads. The
# threshold comes from the roots audit: 28 of the 35 root files with three or more importers that
# are not roots were middle layers; the others are wiring and runtime factories, which carry the
# filename heuristic (`runtime.py`, `app.py`, `main.py`) or answer the hint once.
def root_middle_layer(unit: Unit) -> list[Site]:
    if (
        unit.kind is not FileKind.PRODUCTION
        or not unit.roots
        or is_root_file(unit.path)
        or unit.importers < MIN_IMPORTERS
    ):
        return []
    kinds = ", ".join(sorted(kind.value for kind in unit.roots))
    detail = (
        f"declared a root ({kinds}) and imported by {unit.importers} production modules "
        "that are not roots"
    )
    return [Site(HintId.ROOT_MIDDLE_LAYER, unit.path, min(unit.added), detail)]
