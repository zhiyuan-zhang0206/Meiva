# meiva:note
# The manifest gate judges a plugin only on the faces this process loaded. A child process imports
# `plugin.py` and never `agent_runtime.py` (the runtime face needs the graph and LM stacks), so asking
# the registry for every face used to gate that child against hooks and prompt sections it could not
# see: the manifest declared them, the declaration could not, and the whole plugin was dropped --
# including the SDK wraps it did provide. Every `execute_code` recorded `meiva_to_user` and
# `meiva_memory` as `ContributionMismatch` for this reason (2026-11-06).

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins.enable_config import write_local
from base.packages.plugins.gate import ContributionMismatch
from base.packages.plugins.loading.discovery import SURFACE_MODULE
from base.packages.plugins.loading.faces import import_faces
from base.packages.plugins.loading.registry import build_registry, declarations

_PLUGIN_MODULE_PREFIXES = ("meiva_builtins.plugins.", "plugins.")

_SURFACE = """
from base.packages.plugins.extensions import PluginContributions, SdkWrap


def _passthrough(inner, *args, **kwargs):
    return inner(*args, **kwargs)


def contribute() -> PluginContributions:
    return PluginContributions(sdk_wraps=(SdkWrap("files.write", _passthrough),))
"""

_SECTION_ONLY_RUNTIME = """
from base.packages.plugins.extensions import PluginContributions


def runtime_section(_slices: object, *, catalog: object) -> str:
    return "## Runtime"


def contribute() -> PluginContributions:
    return PluginContributions(system_prompt_sections=(runtime_section,))
"""


def _manifest(name: str, contributions: dict[str, object]) -> str:
    return json.dumps(
        {
            "apiVersion": 2,
            "name": name,
            "version": "1.0.0",
            "engines": {"meiva": ">=0.1.0"},
            "contributions": contributions,
        }
    )


def _write_plugin(unit_home: Path, name: str, *, manifest: str, runtime: str | None) -> Path:
    plugin_dir = paths.plugins_dir(home=unit_home) / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.py").write_text(_SURFACE)
    if runtime is not None:
        (plugin_dir / "agent_runtime.py").write_text(runtime)
    (plugin_dir / "meiva-plugin.json").write_text(manifest)
    return plugin_dir


def _enable(unit_home: Path, name: str) -> None:
    write_local(
        {
            "plugins": {
                **{
                    p.name: {"enabled": False}
                    for p in paths.repo_plugins_dir().iterdir()
                    if p.is_dir() and not p.name.startswith(".") and (p / "plugin.py").exists()
                },
                name: {"enabled": True},
            }
        },
        home=unit_home,
    )


@pytest.fixture(autouse=True)
def _restore_plugin_modules() -> Iterator[None]:
    before = {k: v for k, v in sys.modules.items() if k.startswith(_PLUGIN_MODULE_PREFIXES)}
    yield
    for key in [
        k for k in sys.modules if k.startswith(_PLUGIN_MODULE_PREFIXES) and k not in before
    ]:
        del sys.modules[key]
    sys.modules.update(before)


# meiva:note
# The exec child asks for every face after importing surfaces only: it admits what it can see.
def test_a_face_this_process_did_not_import_does_not_gate_its_keys(unit_home: Path):
    _write_plugin(
        unit_home,
        "child_scope",
        manifest=_manifest(
            "child_scope",
            {
                "sdkWraps": ["files.write"],
                "hooks": ["before_llm"],
                "systemPromptSections": ["runtime_section"],
            },
        ),
        runtime=_SECTION_ONLY_RUNTIME,
    )
    _enable(unit_home, "child_scope")

    failures: list[tuple[str, BaseException]] = []
    import_faces(
        surface=True, report=lambda name, exc: failures.append((name, exc)), home=unit_home
    )
    assert failures == []

    declaration = {d.name: d for d in declarations(home=unit_home)}["child_scope"]
    assert declaration.faces == (SURFACE_MODULE,)
    assert declaration.keys == ("sdkNamespaces", "sdkWraps")

    registry = build_registry(home=unit_home)
    assert [name for name, _contributions in registry.plugins] == ["child_scope"]
    assert [wrap.target for _plugin, wrap in registry.sdk_wraps()] == ["files.write"]


# meiva:note
# Narrowing the keys must not stop the gate from refusing a manifest the loaded faces contradict.
def test_a_loaded_face_is_still_gated_in_both_directions(unit_home: Path):
    _write_plugin(
        unit_home,
        "loaded_scope",
        manifest=_manifest(
            "loaded_scope",
            {
                "sdkWraps": ["files.write"],
                "systemPromptSections": ["runtime_section", "missing_section"],
            },
        ),
        runtime=_SECTION_ONLY_RUNTIME,
    )
    _enable(unit_home, "loaded_scope")

    failures: list[tuple[str, BaseException]] = []
    import_faces(report=lambda name, exc: failures.append((name, exc)), home=unit_home)

    [(name, exc)] = failures
    assert name == "loaded_scope"
    assert isinstance(exc, ContributionMismatch)
    assert "'missing_section' is declared but the plugin does not provide it" in str(exc)
    assert build_registry(home=unit_home).plugins == ()
