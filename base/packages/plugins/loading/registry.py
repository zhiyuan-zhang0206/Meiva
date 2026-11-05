from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from base.host.boundary.untyped import object_of
from base.packages.plugins import load_report
from base.packages.plugins.config_face import CONFIG_FACE
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.gate import check_manifest
from base.packages.plugins.loading import StateGate
from base.packages.plugins.loading.discovery import (
    FACE_MODULE,
    SURFACE_MODULE,
    enabled_plugin_dirs,
    pkg_of,
)
from base.packages.plugins.manifest import load_manifest
from base.telemetry import report_sink_failure

SURFACE_FACES: tuple[str, ...] = (SURFACE_MODULE, CONFIG_FACE)
_PLUGIN_PACKAGES = frozenset({"meiva_builtins.plugins", "plugins"})
ALL_FACES: tuple[str, ...] = (*SURFACE_FACES, FACE_MODULE)

FACE_KEYS: dict[str, tuple[str, ...]] = {
    SURFACE_MODULE: ("sdkNamespaces", "sdkWraps"),
    CONFIG_FACE: ("config",),
    FACE_MODULE: ("hooks", "systemPromptSections", "compactReminderSections"),
}


# meiva:note
# One plugin as this process loaded it: what it declared, and which faces produced that. `faces` is the
# gate's scope; a face the caller asked for but this process never imported (a child loads surfaces
# only) contributes nothing, so the gate judges a plugin only on the faces whose declaration it sees.
@dataclass(frozen=True)
class PluginDeclaration:
    name: str
    plugin_dir: Path
    contributions: PluginContributions
    faces: tuple[str, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(key for face in self.faces for key in FACE_KEYS.get(face, ()))


def _declared(plugin: str, face: str, contribute: object) -> PluginContributions:
    if not callable(contribute):
        raise TypeError(f"{plugin}.{face}: contribute is not callable")
    contributions = object_of(contribute)
    if not isinstance(contributions, PluginContributions):
        raise TypeError(
            f"{plugin}.{face}.contribute() returned {type(contributions).__name__}, "
            "not PluginContributions"
        )
    return contributions


# meiva:link base/packages/plugins/docs/runtime-contributions.meiva.okf.md
# meiva:note
# Declare each enabled plugin from the faces of `faces` this process has already imported. A face is
# listed only when its module is present and its `contribute()` ran, so the answer is what this process
# can actually see, not what the caller asked for.
def declarations(
    faces: Sequence[str] = ALL_FACES, report: load_report.Reporter | None = None, *, home: Path
) -> list[PluginDeclaration]:
    found: list[PluginDeclaration] = []
    for name, plugin_dir in enabled_plugin_dirs(report, home=home):
        merged: PluginContributions | None = None
        loaded: list[str] = []
        try:
            load_manifest(plugin_dir)
            for face in faces:
                module = sys.modules.get(f"{pkg_of(plugin_dir)}.{name}.{face}")
                contribute = object_of(getattr, module, "contribute", None)
                if contribute is None:
                    continue
                declared = _declared(name, face, contribute)
                merged = declared if merged is None else merged.merged(declared)
                loaded.append(face)
        except Exception as exc:
            load_report.reporter(report)(name, exc)
            continue
        if merged is not None:
            found.append(PluginDeclaration(name, plugin_dir, merged, tuple(loaded)))
    return found


def loaded_state_classes() -> frozenset[type[BaseModel]]:
    classes: set[type[BaseModel]] = set()
    for dotted, module in tuple(sys.modules.items()):
        parts = dotted.split(".")
        if parts[-1] != FACE_MODULE or ".".join(parts[:-2]) not in _PLUGIN_PACKAGES:
            continue
        contribute = object_of(getattr, module, "contribute", None)
        if contribute is None:
            continue
        try:
            classes.update(_declared(parts[-2], FACE_MODULE, contribute).state)
        except Exception as exc:
            report_sink_failure(
                f"plugin {parts[-2]} agent_runtime face (its state classes are missing "
                "from the serializer allowlist)",
                exc,
            )
    return frozenset(classes)


# meiva:link base/packages/plugins/docs/runtime-contributions.meiva.okf.md
# meiva:note
# Admit the enabled plugins whose manifest agrees with the faces this process loaded. Each plugin is
# judged against its own faces alone (`PluginDeclaration.keys`), so asking for every face does not make
# a process gate a face it never imported.
def build_registry(
    faces: Sequence[str] = ALL_FACES,
    report: load_report.Reporter | None = None,
    *,
    validate_state: StateGate | None = None,
    home: Path,
) -> ExtensionRegistry:
    admitted: list[tuple[str, PluginContributions]] = []
    for declaration in declarations(faces, report, home=home):
        name, plugin_dir, contributions = (
            declaration.name,
            declaration.plugin_dir,
            declaration.contributions,
        )
        if contributions.state and validate_state is None:
            raise RuntimeError(
                f"plugin {name!r} declares state classes but this load supplied no state gate "
                "(`validate_state`); only a caller that runs agent code can load an agent-runtime face"
            )
        try:
            if contributions.state and validate_state is not None:
                validate_state(ExtensionRegistry(((name, contributions),)))
            check_manifest(name, plugin_dir, contributions, declaration.keys)
        except Exception as exc:
            load_report.reporter(report)(name, exc)
            continue
        admitted.append((name, contributions))
    return ExtensionRegistry(tuple(admitted))
