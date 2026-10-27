# meiva:note
# The service-locator shapes that fetch a component's inputs from the application.
# Ambient acquisitions belong to meizel check. This hint recognizes settings roots
# from the same manifest revision as the source, with the legacy filename heuristic
# retained for existing process entries. The same declarations tell a root from a middle layer.
from __future__ import annotations

import ast
from collections.abc import Sequence

from meizel.graph.model import AmbientKind
from scripts.lint.diagnostics.diff_hints.model import (
    MIN_IMPORTERS,
    MIN_ONWARD,
    FileKind,
    HintId,
    NewRoot,
    Site,
    Unit,
)
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


_NAMED = 5


def _named(paths: tuple[str, ...]) -> str:
    shown = ", ".join(f"`{path}`" for path in paths[:_NAMED])
    return shown + (f" and {len(paths) - _NAMED} more" if len(paths) > _NAMED else "")


# meiva:note
# A file declared under `[roots]` that other modules import is a middle layer, not a composition
# root: every importer reads the world through it, and a test cannot vary what it reads. It is asked
# about when the change touches it and two or more production modules that are not roots import it,
# or one such module does and three or more modules import that one. Wiring and runtime factories are
# the answers to give (`Hint-Ack`), and the names `runtime.py`, `app.py` and `main.py` are not asked.
def root_middle_layer(unit: Unit) -> list[Site]:
    if unit.kind is not FileKind.PRODUCTION or not unit.roots or is_root_file(unit.path):
        return []
    kinds = ", ".join(sorted(kind.value for kind in unit.roots))
    direct = unit.importers.direct
    detail = None
    if len(direct) >= MIN_IMPORTERS:
        detail = (
            f"declared a root ({kinds}) and imported by {len(direct)} production modules that "
            f"are not roots: {_named(direct)}"
        )
    else:
        for importer, count in unit.importers.onward:
            if count >= MIN_ONWARD:
                detail = (
                    f"declared a root ({kinds}) and imported by `{importer}`, which {count} "
                    "production modules that are not roots import"
                )
    return (
        []
        if detail is None
        else [Site(HintId.ROOT_MIDDLE_LAYER, unit.path, min(unit.added), detail)]
    )


# meiva:note
# A root the change declares in a manifest is always asked about, whether or not the file changed:
# the declaration is the move that turns a middle layer into a "root" for the checker. The answer
# says what it is (a process entry, a factory the process builds once, the wiring) and who calls it.
def new_root_sites(new_roots: Sequence[NewRoot]) -> list[Site]:
    sites: list[Site] = []
    for new in new_roots:
        kinds = ", ".join(sorted(kind.value for kind in new.kinds))
        if new.importers is None:
            seen = "its importers are not read for this language"
        elif new.importers.direct:
            seen = (
                f"{len(new.importers.direct)} production modules that are not roots import it: "
                f"{_named(new.importers.direct)}"
            )
        else:
            seen = "no production module that is not a root imports it"
        detail = f"declares a root ({kinds}) in {new.manifest}; {seen}"
        sites.append(Site(HintId.ROOT_MIDDLE_LAYER, new.path, 1, detail))
    return sites
