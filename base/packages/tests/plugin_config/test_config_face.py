from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.catalog import ModelCatalog
from base.packages.plugins import config_face
from base.packages.plugins.config_registration import (
    disk_image_path,
    overlay_config_classes,
    plugin_bootstrap_config,
    resolve_overlay_targets,
    validate_config_overlay,
)
from base.packages.plugins.enable_config import discover_plugins, write_local
from base.packages.plugins.plugin_errors import (
    InvalidConfigData,
    InvalidConfigOverlay,
    SchemaDriftError,
)

_FACE = (
    "from pydantic import BaseModel, Field\n"
    "from base.packages.plugins.extensions import PluginContributions\n"
    "class Config(BaseModel):\n"
    "    marker: str = Field(default='.git', json_schema_extra={'per_agent': True})\n"
    "    fixed: int = 1\n"
    "def contribute():\n"
    "    return PluginContributions(config=Config)\n"
)


def _repo_dir(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_plugins"
    repo.mkdir(exist_ok=True)
    return repo


def _user_dir(tmp_path: Path) -> Path:
    user = tmp_path / "user_plugins"
    user.mkdir(exist_ok=True)
    return user


def _discovered(tmp_path: Path) -> dict[str, Path]:
    return discover_plugins(repo_dir=_repo_dir(tmp_path), user_dir=_user_dir(tmp_path))


def _plugin(tmp_path: Path, name: str, face: str | None) -> Path:
    plugin_dir = _user_dir(tmp_path) / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(f"__description__ = '{name}'\n")
    if face is not None:
        (plugin_dir / "default_config.py").write_text(face)
    return plugin_dir


def test_the_declared_class_is_read_from_the_face(tmp_path: Path) -> None:
    plugin_dir = _plugin(tmp_path, "probe", _FACE)
    cls = config_face.declared_config_class("probe", plugin_dir)
    assert cls is not None
    assert list(cls.model_fields) == ["marker", "fixed"]


def test_a_plugin_without_a_face_declares_no_class(tmp_path: Path) -> None:
    assert config_face.declared_config_class("bare", _plugin(tmp_path, "bare", None)) is None


@pytest.mark.parametrize(
    "extra",
    [
        "def contribute():\n    return PluginContributions(config=Config, state=Config)\n",
        "def contribute():\n    return 1\n",
    ],
)
def test_a_config_face_rejects_other_contribution_surfaces(tmp_path: Path, extra: str) -> None:
    body = _FACE.split("def contribute", maxsplit=1)[0] + extra
    with pytest.raises((ValueError, TypeError)):
        config_face.declared_config_class("probe", _plugin(tmp_path, "probe", body))


def test_overlay_validation_recognizes_the_enabled_plugins_config(
    tmp_path: Path,
    model_catalog: ModelCatalog,
) -> None:
    _plugin(tmp_path, "probe", _FACE)
    write_local({"plugins": {"probe": {"enabled": True}}}, home=tmp_path)
    dirs = _discovered(tmp_path)

    assert overlay_config_classes(plugin_dirs=dirs, home=tmp_path)["probe"].__name__ == "Config"
    assert resolve_overlay_targets({"marker": "x"}, plugin_dirs=dirs, home=tmp_path) == {
        "marker": ("probe", "marker")
    }
    validate_config_overlay(
        {"marker": "x"}, models=model_catalog.models, plugin_dirs=dirs, home=tmp_path
    )
    with pytest.raises(InvalidConfigOverlay, match="type validation"):
        validate_config_overlay(
            {"marker": 3}, models=model_catalog.models, plugin_dirs=dirs, home=tmp_path
        )
    with pytest.raises(InvalidConfigOverlay, match="per_agent"):
        resolve_overlay_targets({"fixed": 2}, plugin_dirs=dirs, home=tmp_path)


def test_overlay_validation_ignores_a_disabled_plugin(tmp_path: Path) -> None:
    _plugin(tmp_path, "probe", _FACE)
    write_local({"plugins": {"probe": {"enabled": False}}}, home=tmp_path)
    dirs = _discovered(tmp_path)

    assert "probe" not in overlay_config_classes(plugin_dirs=dirs, home=tmp_path)
    with pytest.raises(InvalidConfigOverlay, match="not in framework Settings"):
        resolve_overlay_targets({"marker": "x"}, plugin_dirs=dirs, home=tmp_path)


def test_a_broken_face_is_left_out_not_raised(tmp_path: Path) -> None:
    _plugin(tmp_path, "probe", _FACE)
    _plugin(tmp_path, "broken", "raise RuntimeError('boom')\n")
    write_local(
        {"plugins": {"probe": {"enabled": True}, "broken": {"enabled": True}}},
        home=tmp_path,
    )
    dirs = _discovered(tmp_path)

    assert sorted(overlay_config_classes(plugin_dirs=dirs, home=tmp_path)) == ["probe"]


def test_a_pure_face_admits_config_and_core_dependencies(tmp_path: Path) -> None:
    body = _FACE.replace(
        "config=Config)", "config=Config, flags=('daemon.notice_ttl_limit_seconds',))"
    )
    declaration = config_face.configuration_declaration("probe", _plugin(tmp_path, "probe", body))
    assert declaration.config is not None
    assert declaration.flags == ("daemon.notice_ttl_limit_seconds",)


def test_a_dependency_only_face_needs_no_config_class(tmp_path: Path) -> None:
    body = _FACE.replace("config=Config", "flags=('daemon.notice_ttl_limit_seconds',)")
    plugin_dir = _plugin(tmp_path, "probe", body)
    assert config_face.declared_config_class("probe", plugin_dir) is None
    assert config_face.configuration_declaration("probe", plugin_dir).flags


@pytest.mark.parametrize("key", ["data_plane.db_url", "daemon.not_a_field", "a.b.c"])
def test_pure_admission_rejects_sensitive_and_unknown_core_dependencies(
    tmp_path: Path, key: str
) -> None:
    from base.packages.plugins.flags import UnknownFlag

    body = _FACE.replace("config=Config)", f"config=Config, flags=({key!r},))")
    with pytest.raises(UnknownFlag):
        config_face.configuration_declaration("probe", _plugin(tmp_path, "probe", body))


def _cluster_face(*, sensitive: bool = False) -> str:
    return _FACE.replace(
        "json_schema_extra={'per_agent': True}",
        f"json_schema_extra={{'scope': 'cluster-default', 'sensitive': {sensitive}}}",
    )


def test_bootstrap_packet_reads_fresh_authority_and_only_declared_cluster_fields(
    tmp_path: Path,
) -> None:
    _plugin(tmp_path, "probe", _cluster_face())
    dirs = _discovered(tmp_path)
    image = disk_image_path("probe", home=tmp_path)
    assert json.loads(plugin_bootstrap_config(plugin_dirs=dirs, home=tmp_path)) == {
        "probe": {"marker": ".git"}
    }
    assert not image.exists()
    image.parent.mkdir(parents=True)
    image.write_text('{"marker":"first","fixed":2}')
    assert json.loads(plugin_bootstrap_config(plugin_dirs=dirs, home=tmp_path)) == {
        "probe": {"marker": "first"}
    }
    image.write_text('{"marker":"second","fixed":3}')
    assert json.loads(plugin_bootstrap_config(plugin_dirs=dirs, home=tmp_path)) == {
        "probe": {"marker": "second"}
    }


def test_bootstrap_packet_rejects_sensitive_cluster_declarations(
    tmp_path: Path,
) -> None:
    _plugin(tmp_path, "probe", _cluster_face(sensitive=True))
    dirs = _discovered(tmp_path)
    with pytest.raises(ValueError, match=r"probe\.marker may not carry secrets"):
        plugin_bootstrap_config(plugin_dirs=dirs, home=tmp_path)


@pytest.mark.parametrize("content", ['{"marker":"only"}', '{"marker":1,"fixed":2}', "bad json"])
def test_bootstrap_packet_preserves_authority_schema_admission(
    tmp_path: Path, content: str
) -> None:
    _plugin(tmp_path, "probe", _cluster_face())
    dirs = _discovered(tmp_path)
    image = disk_image_path("probe", home=tmp_path)
    image.parent.mkdir(parents=True)
    image.write_text(content)
    with pytest.raises((InvalidConfigData, SchemaDriftError)):
        plugin_bootstrap_config(plugin_dirs=dirs, home=tmp_path)
