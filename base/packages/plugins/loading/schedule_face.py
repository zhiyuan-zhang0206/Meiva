# meiva:note
# Loader for plugin schedules and dynamic UI stats faces.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path
from typing import TypeGuard

from pydantic import BaseModel

from base.packages.plugins import config_face, enable_config, load_report
from base.packages.plugins.manifest import load_manifest
from base.packages.plugins.surface.schedules import ScheduleSpec, StatCardSpec


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def resolve_plugin_config_for_face(
    plugin: str, plugin_dir: Path, *, home: Path
) -> BaseModel | None:
    cls = config_face.declared_config_class(plugin, plugin_dir)
    if cls is None:
        return None
    return config_face.read_config_image(cls, config_face.disk_image_path(plugin, home=home))


def declared_schedules(plugin: str, plugin_dir: Path, *, home: Path) -> tuple[ScheduleSpec, ...]:
    manifest = load_manifest(plugin_dir)
    sched_decl: object = manifest.contributions.get("schedules")
    if not _is_object_dict(sched_decl) or "face" not in sched_decl:
        return ()

    raw_face = sched_decl.get("face")
    if not isinstance(raw_face, str):
        return ()
    face_name: str = raw_face
    face_py = plugin_dir / face_name
    if not face_py.is_file():
        raise FileNotFoundError(f"schedules face file {face_py} not found for plugin {plugin!r}")

    cfg = resolve_plugin_config_for_face(plugin, plugin_dir, home=home)
    module = _load_face_module(f"plugins.{plugin}._schedules_face", face_py)
    contribute = getattr(module, "contribute", None)
    if contribute is None or not callable(contribute):
        raise AttributeError(f"{face_py} has no contribute()")

    sig = inspect.signature(contribute)
    res: object = contribute(cfg) if len(sig.parameters) > 0 else contribute()

    if not _is_tuple(res):
        raise TypeError(
            f"{face_py} contribute() must return a tuple of ScheduleSpec instances, got {type(res).__name__}"
        )

    specs_list: list[ScheduleSpec] = []
    for item in res:
        if not isinstance(item, ScheduleSpec):
            raise TypeError(
                f"{face_py} contribute() must return a tuple of ScheduleSpec instances, got element of type {type(item).__name__}"
            )
        specs_list.append(item)
    specs: tuple[ScheduleSpec, ...] = tuple(specs_list)
    _validate_schedule_specs(plugin, specs)
    return specs


def _validate_schedule_specs(plugin: str, specs: tuple[ScheduleSpec, ...]) -> None:
    prefix_colon = f"{plugin}:"
    prefix_dash = f"{plugin}-"
    seen_names: set[str] = set()

    for spec in specs:
        if not (spec.name.startswith(prefix_colon) or spec.name.startswith(prefix_dash)):
            raise ValueError(
                f"schedule name {spec.name!r} must start with {prefix_colon!r} or {prefix_dash!r}"
            )
        if spec.name in seen_names:
            raise ValueError(f"duplicate schedule name {spec.name!r} in plugin {plugin!r}")
        seen_names.add(spec.name)
        try:
            compile(spec.script, f"<{plugin}:{spec.name}>", "exec")
        except SyntaxError as exc:
            raise ValueError(
                f"schedule {spec.name!r} script has Python syntax errors: {exc}"
            ) from exc


def declared_ui_stats(plugin: str, plugin_dir: Path, *, home: Path) -> tuple[StatCardSpec, ...]:
    manifest = load_manifest(plugin_dir)
    ui_decl: object = manifest.contributions.get("ui")
    if not _is_object_dict(ui_decl) or "statsFace" not in ui_decl:
        return ()

    raw_face = ui_decl.get("statsFace")
    if not isinstance(raw_face, str):
        return ()
    face_name: str = raw_face
    face_py = plugin_dir / face_name
    if not face_py.is_file():
        raise FileNotFoundError(f"ui statsFace file {face_py} not found for plugin {plugin!r}")

    cfg = resolve_plugin_config_for_face(plugin, plugin_dir, home=home)
    module = _load_face_module(f"plugins.{plugin}._ui_stats_face", face_py)
    contribute = getattr(module, "contribute", None)
    if contribute is None or not callable(contribute):
        raise AttributeError(f"{face_py} has no contribute()")

    sig = inspect.signature(contribute)
    res: object = contribute(cfg) if len(sig.parameters) > 0 else contribute()

    if not _is_tuple(res):
        raise TypeError(
            f"{face_py} contribute() must return a tuple of StatCardSpec instances, got {type(res).__name__}"
        )

    cards_list: list[StatCardSpec] = []
    for card_item in res:
        if not isinstance(card_item, StatCardSpec):
            raise TypeError(
                f"{face_py} contribute() must return a tuple of StatCardSpec instances, got element of type {type(card_item).__name__}"
            )
        cards_list.append(card_item)
    cards: tuple[StatCardSpec, ...] = tuple(cards_list)

    seen_ids: set[str] = set()
    for card in cards:
        if card.id in seen_ids:
            raise ValueError(f"duplicate stat card id {card.id!r} in plugin {plugin!r}")
        seen_ids.add(card.id)

    return cards


def load_enabled_plugin_schedules(
    *, home: Path, report: load_report.Reporter | None = None
) -> tuple[dict[str, tuple[ScheduleSpec, ...]], set[str]]:
    installed = enable_config.installed_plugin_dirs(home=home)
    config = enable_config.load_for_runtime(set(installed), home=home)
    specs_by_plugin: dict[str, tuple[ScheduleSpec, ...]] = {}
    failed_plugins: set[str] = set()
    reporter = load_report.reporter(report)

    for plugin, plugin_dir in sorted(installed.items()):
        entry = config.plugins.get(plugin)
        if entry is None or not entry.enabled:
            continue
        try:
            manifest = load_manifest(plugin_dir)
            if "schedules" not in manifest.contributions:
                continue
            specs = declared_schedules(plugin, plugin_dir, home=home)
            specs_by_plugin[plugin] = specs
        except Exception as exc:
            reporter(plugin, exc)
            failed_plugins.add(plugin)

    return specs_by_plugin, failed_plugins


def load_plugin_ui_stats_safe(
    plugin: str,
    plugin_dir: Path,
    *,
    home: Path,
    report: load_report.Reporter | None = None,
) -> tuple[StatCardSpec, ...]:
    try:
        return declared_ui_stats(plugin, plugin_dir, home=home)
    except Exception as exc:
        load_report.reporter(report)(plugin, exc)
        return ()


def _load_face_module(dotted: str, face_py: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(dotted, face_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load face module {face_py}")
    module = importlib.util.module_from_spec(spec)
    held = sys.modules.get(dotted)
    sys.modules[dotted] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if held is None:
            sys.modules.pop(dotted, None)
        else:
            sys.modules[dotted] = held
    return module
