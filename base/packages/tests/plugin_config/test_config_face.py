from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from base import paths
from base.lm.catalog import ModelCatalog
from base.packages.plugins import config_face
from base.packages.plugins.config_registration import (
    overlay_config_classes,
    plugin_bootstrap_config,
    resolve_overlay_targets,
    validate_config_overlay,
)
from base.packages.plugins.enable_config import discover_plugins, write_local
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.paths import meiva_home

_FACE = (
    "from pydantic import BaseModel, Field\n"
    "from base.packages.plugins.extensions import PluginContributions\n"
    "class Config(BaseModel):\n"
    "    marker: str = Field(default='.git', json_schema_extra={'per_agent': True})\n"
    "    fixed: int = 1\n"
    "def contribute():\n"
    "    return PluginContributions(config=Config)\n"
)


@pytest.fixture(autouse=True)
def _isolate_paths(model_catalog: ModelCatalog, unit_home: Path) -> None:
    paths.plugins_dir(home=unit_home).mkdir(parents=True, exist_ok=True)


def _write_enabled(raw: dict[str, dict[str, dict[str, bool]]]) -> None:
    states = {name: {"enabled": False} for name in discover_plugins(home=meiva_home())}
    write_local({"plugins": {**states, **raw["plugins"]}}, home=meiva_home())


def _plugin(name: str, face: str | None) -> Path:
    plugin_dir = paths.plugins_dir(home=meiva_home()) / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(f"__description__ = '{name}'\n")
    if face is not None:
        (plugin_dir / "default_config.py").write_text(face)
    return plugin_dir


def test_the_declared_class_is_read_from_the_face() -> None:
    plugin_dir = _plugin("probe", _FACE)
    cls = config_face.declared_config_class("probe", plugin_dir)
    assert cls is not None
    assert list(cls.model_fields) == ["marker", "fixed"]


def test_a_plugin_without_a_face_declares_no_class() -> None:
    assert config_face.declared_config_class("bare", _plugin("bare", None)) is None


@pytest.mark.parametrize(
    "extra",
    [
        "def contribute():\n    return PluginContributions(config=Config, state=Config)\n",
        "def contribute():\n    return 1\n",
    ],
)
def test_a_config_face_rejects_other_contribution_surfaces(extra: str) -> None:
    body = _FACE.split("def contribute", maxsplit=1)[0] + extra
    with pytest.raises((ValueError, TypeError)):
        config_face.declared_config_class("probe", _plugin("probe", body))


def test_overlay_validation_recognizes_the_enabled_plugins_config(
    model_catalog: ModelCatalog,
) -> None:
    _plugin("probe", _FACE)
    _write_enabled({"plugins": {"probe": {"enabled": True}}})

    assert overlay_config_classes(home=meiva_home())["probe"].__name__ == "Config"
    assert resolve_overlay_targets({"marker": "x"}, home=meiva_home()) == {
        "marker": ("probe", "marker")
    }
    validate_config_overlay({"marker": "x"}, models=model_catalog.models, home=meiva_home())
    with pytest.raises(InvalidConfigOverlay, match="type validation"):
        validate_config_overlay({"marker": 3}, models=model_catalog.models, home=meiva_home())
    with pytest.raises(InvalidConfigOverlay, match="per_agent"):
        resolve_overlay_targets({"fixed": 2}, home=meiva_home())


def test_overlay_validation_ignores_a_disabled_plugin() -> None:
    _plugin("probe", _FACE)
    _write_enabled({"plugins": {"probe": {"enabled": False}}})

    assert "probe" not in overlay_config_classes(home=meiva_home())
    with pytest.raises(InvalidConfigOverlay, match="not in framework Settings"):
        resolve_overlay_targets({"marker": "x"}, home=meiva_home())


def test_a_broken_face_is_left_out_not_raised() -> None:
    _plugin("probe", _FACE)
    _plugin("broken", "raise RuntimeError('boom')\n")
    _write_enabled({"plugins": {"probe": {"enabled": True}, "broken": {"enabled": True}}})

    assert sorted(overlay_config_classes(home=meiva_home())) == ["probe"]


def test_a_pure_face_admits_config_and_core_dependencies() -> None:
    body = _FACE.replace(
        "config=Config)", "config=Config, flags=('daemon.notice_ttl_limit_seconds',))"
    )
    declaration = config_face.configuration_declaration("probe", _plugin("probe", body))
    assert declaration.config is not None
    assert declaration.flags == ("daemon.notice_ttl_limit_seconds",)


def test_a_dependency_only_face_needs_no_config_class() -> None:
    body = _FACE.replace("config=Config", "flags=('daemon.notice_ttl_limit_seconds',)")
    plugin_dir = _plugin("probe", body)
    assert config_face.declared_config_class("probe", plugin_dir) is None
    assert config_face.configuration_declaration("probe", plugin_dir).flags


@pytest.mark.parametrize("key", ["data_plane.db_url", "daemon.not_a_field", "a.b.c"])
def test_pure_admission_rejects_sensitive_and_unknown_core_dependencies(key: str) -> None:
    from base.packages.plugins.flags import UnknownFlag

    body = _FACE.replace("config=Config)", f"config=Config, flags=({key!r},))")
    with pytest.raises(UnknownFlag):
        config_face.configuration_declaration("probe", _plugin("probe", body))


@pytest.fixture
def bootstrap_authority_home(unit_home: Path) -> dict[str, object]:
    import json

    baseline: dict[str, object] = json.loads(plugin_bootstrap_config(home=unit_home))
    assert "probe" not in baseline
    return baseline


def _cluster_face(*, sensitive: bool = False) -> str:
    return _FACE.replace(
        "json_schema_extra={'per_agent': True}",
        f"json_schema_extra={{'scope': 'cluster-default', 'sensitive': {sensitive}}}",
    )


def test_bootstrap_packet_reads_fresh_authority_and_only_declared_cluster_fields(
    bootstrap_authority_home: dict[str, object],
) -> None:
    import json

    from base.packages.plugins.config_registration import disk_image_path

    _plugin("probe", _cluster_face())
    image = disk_image_path("probe", home=meiva_home())
    assert json.loads(plugin_bootstrap_config(home=meiva_home())) == {
        **bootstrap_authority_home,
        "probe": {"marker": ".git"},
    }
    assert not image.exists()
    image.parent.mkdir(parents=True)
    image.write_text('{"marker":"first","fixed":2}')
    assert json.loads(plugin_bootstrap_config(home=meiva_home())) == {
        **bootstrap_authority_home,
        "probe": {"marker": "first"},
    }
    image.write_text('{"marker":"second","fixed":3}')
    assert json.loads(plugin_bootstrap_config(home=meiva_home())) == {
        **bootstrap_authority_home,
        "probe": {"marker": "second"},
    }


def test_bootstrap_packet_rejects_sensitive_cluster_declarations(
    bootstrap_authority_home: dict[str, object],
) -> None:
    _plugin("probe", _cluster_face(sensitive=True))
    with pytest.raises(ValueError, match=r"probe\.marker may not carry secrets"):
        plugin_bootstrap_config(home=meiva_home())


@pytest.mark.parametrize("content", ['{"marker":"only"}', '{"marker":1,"fixed":2}', "bad json"])
def test_bootstrap_packet_preserves_authority_schema_admission(
    bootstrap_authority_home: dict[str, object], content: str
) -> None:
    from base.packages.plugins.config_registration import disk_image_path
    from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError

    _plugin("probe", _cluster_face())
    image = disk_image_path("probe", home=meiva_home())
    image.parent.mkdir(parents=True)
    image.write_text(content)
    with pytest.raises((InvalidConfigData, SchemaDriftError)):
        plugin_bootstrap_config(home=meiva_home())


# meiva:note
# A face whose model names a sibling class. `from __future__ import annotations` is what makes the
# reference a string that Pydantic resolves through `sys.modules[Config.__module__]` at class
# creation, and the sleep is what holds an execution inside that resolution window long enough for a
# second reader of the same file to register itself beside it.
_RACED_FACE = (
    "from __future__ import annotations\n"
    "\n"
    "import time\n"
    "\n"
    "from pydantic import BaseModel\n"
    "\n"
    "from base.packages.plugins.extensions import PluginContributions\n"
    "\n"
    "time.sleep(0.002)\n"
    "\n"
    "class Collector(BaseModel):\n"
    "    label: str = 'x'\n"
    "\n"
    "class Config(BaseModel):\n"
    "    collectors: tuple[Collector, ...] = ()\n"
    "\n"
    "def contribute():\n"
    "    return PluginContributions(config=Config)\n"
)

_READERS = 4
_ROUNDS = 10


# meiva:note
# A second reader of a face must not steer the first one's annotations: readers run on dispatch
# threads (a config read and a config write both build the panel metadata), so two executions of one
# file do overlap in a live daemon.
def test_concurrent_readers_of_one_face_each_get_a_class_they_can_instantiate() -> None:
    plugin_dir = _plugin("raced", _RACED_FACE)
    barrier = threading.Barrier(_READERS, timeout=30)

    def read_face() -> None:
        barrier.wait()
        for _ in range(_ROUNDS):
            cls = config_face.declared_config_class("raced", plugin_dir)
            assert cls is not None, "the raced face declares a config"
            cls()

    with ThreadPoolExecutor(max_workers=_READERS) as pool:
        readers = [pool.submit(read_face) for _ in range(_READERS)]
        for reader in readers:
            reader.result()
