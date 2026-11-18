from __future__ import annotations

import json
from pathlib import Path

import pytest

from base import paths
from base.packages.plugins.enable_config import write_local
from base.packages.plugins.loading.ui_declarations import (
    InvalidPluginManifestError,
    declared_stat_cards,
)

_FACE = (
    "from base.packages.plugins.surface.schedules import StatCardSpec\n"
    "def contribute():\n"
    "    return (StatCardSpec(id='from-face', label='From face'),)\n"
)


def _plugin(
    home: Path, name: str, ui: dict[str, object] | None, *, face: str | None = None
) -> None:
    directory = paths.plugins_dir(home=home) / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text('"""A test plugin."""\n', encoding="utf-8")
    if face is not None:
        (directory / "ui_stats.py").write_text(face, encoding="utf-8")
    manifest = {
        "apiVersion": 2,
        "name": name.replace("_", "-"),
        "version": "1.0.0",
        "engines": {"meiva": ">=0.1.0"},
        "contributions": {} if ui is None else {"ui": ui},
    }
    (directory / "meiva-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")


def _enable(home: Path, **enabled: bool) -> None:
    write_local({"plugins": {name: {"enabled": on} for name, on in enabled.items()}}, home=home)


def test_the_declared_cards_are_those_of_enabled_plugins_only(unit_home: Path) -> None:
    _plugin(unit_home, "usage", {"stats": [{"id": "quota", "label": "Quota"}]})
    _plugin(unit_home, "retired", {"stats": [{"id": "old", "label": "Old"}]})
    _plugin(unit_home, "skins", {"themes": []})
    _plugin(unit_home, "plain", None)
    _enable(unit_home, usage=True, retired=False, skins=True, plain=True)

    assert declared_stat_cards(home=unit_home) == {("usage", "quota")}


def test_a_stats_face_declares_cards_beside_the_manifest_entries(unit_home: Path) -> None:
    _plugin(
        unit_home,
        "usage",
        {"stats": [{"id": "quota", "label": "Quota"}], "statsFace": "ui_stats.py"},
        face=_FACE,
    )
    _enable(unit_home, usage=True)

    assert declared_stat_cards(home=unit_home) == {("usage", "quota"), ("usage", "from-face")}


def test_a_stats_face_that_fails_declares_nothing_and_is_reported_to_the_caller(
    unit_home: Path,
) -> None:
    _plugin(
        unit_home,
        "usage",
        {"stats": [{"id": "quota", "label": "Quota"}], "statsFace": "ui_stats.py"},
        face="raise RuntimeError('broken face')\n",
    )
    _enable(unit_home, usage=True)
    reports: list[tuple[str, str]] = []

    declared = declared_stat_cards(
        home=unit_home, report=lambda plugin, exc: reports.append((plugin, str(exc)))
    )

    assert declared == {("usage", "quota")}
    assert reports == [("usage", "broken face")]


def test_an_enabled_plugin_with_an_invalid_manifest_is_named_not_skipped(
    unit_home: Path,
) -> None:
    _plugin(unit_home, "usage", None)
    (paths.plugins_dir(home=unit_home) / "usage" / "meiva-plugin.json").write_text("{", "utf-8")
    _enable(unit_home, usage=True)

    with pytest.raises(InvalidPluginManifestError, match="plugin 'usage' has an invalid"):
        declared_stat_cards(home=unit_home)
