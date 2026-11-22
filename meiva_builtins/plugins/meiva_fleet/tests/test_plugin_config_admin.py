import json
from collections.abc import Generator
from pathlib import Path

import pytest

from base.host.env.tests.env_files import unit_env_path
from base.packages.plugin_config_admin import (
    patch_owner,
    plugin_metadata,
    write_plugin_patch,
)
from base.packages.plugin_config_images import (
    PluginConfigChangedError,
    PluginConfigOwner,
    image_digest,
    write_config_image,
)
from base.packages.plugins.config_registration import disk_image_path
from base.packages.plugins.plugin_errors import InvalidConfigOverlay
from base.paths import meiva_home, plugins_dir
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig


@pytest.fixture(autouse=True)
def preserve_env_file() -> Generator[None]:
    env = unit_env_path()
    previous = env.read_bytes() if env.exists() else None
    try:
        yield
    finally:
        if previous is None:
            env.unlink(missing_ok=True)
        else:
            env.write_bytes(previous)


def owner(path: Path) -> PluginConfigOwner:
    return PluginConfigOwner("meiva_fleet", FleetConfig, path)


def test_metadata_uses_plugin_owner_and_preserves_host_cluster_policy() -> None:
    fields = {
        field.name: field
        for field in plugin_metadata(home=meiva_home())
        if field.owner == "meiva_fleet"
    }
    assert set(fields) == set(FleetConfig.model_fields)
    assert fields["reduce_context_switch"].scope == "cluster-pinned"
    assert fields["reduce_context_switch"].per_agent is False
    assert fields["reduce_context_switch"].restart_required == "agent"
    assert all(not field.sensitive for field in fields.values())


def test_mixed_owner_patch_fails_before_any_image_write(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    with pytest.raises(InvalidConfigOverlay, match="mixes owners"):
        patch_owner({"reduce_context_switch", "llm_model"}, home=meiva_home())
    assert not image.exists()
    actual = patch_owner({"reduce_context_switch"}, home=meiva_home())
    assert actual is not None and actual.name == "meiva_fleet"
    assert patch_owner({"llm_model"}, home=meiva_home()) is None


def test_unknown_patch_field_is_a_request_rejection() -> None:
    with pytest.raises(InvalidConfigOverlay, match="unknown field"):
        patch_owner({"unknown_plugin_config_field"}, home=meiva_home())


_SECOND_FACE = """
from pydantic import BaseModel, Field

from base.packages.plugins.extensions import PluginContributions


class SecondFleetConfig(BaseModel):
    reduce_context_switch: bool = Field(default=True, json_schema_extra={"scope": "cluster-pinned"})


def contribute() -> PluginContributions:
    return PluginContributions(config=SecondFleetConfig)
"""


def test_duplicate_declaration_is_not_a_request_rejection(unit_home: Path) -> None:
    second = plugins_dir(home=unit_home) / "second_fleet"
    second.mkdir(parents=True)
    (second / "plugin.py").write_text("")
    (second / "default_config.py").write_text(_SECOND_FACE)
    with pytest.raises(ValueError, match="more than one declaration owner") as refused:
        patch_owner({"reduce_context_switch"}, home=unit_home)
    assert not isinstance(refused.value, InvalidConfigOverlay)
    assert not disk_image_path("meiva_fleet", home=unit_home).exists()
    assert not disk_image_path("second_fleet", home=unit_home).exists()


def test_image_patch_validates_whole_candidate_and_cas(tmp_path: Path) -> None:
    image = tmp_path / "config.json"
    declaration = owner(image)
    write_plugin_patch(declaration, {"reduce_context_switch": False}, set(), expected_digest=None)
    saved = image.read_bytes()
    revision = image_digest(image)
    with pytest.raises(InvalidConfigOverlay):
        write_plugin_patch(
            declaration, {"reduce_context_switch": "nonsense"}, set(), expected_digest=None
        )
    assert image.read_bytes() == saved
    write_plugin_patch(
        declaration, {"reduce_context_switch": True}, set(), expected_digest=revision
    )
    saved = image.read_bytes()
    with pytest.raises(PluginConfigChangedError, match="changed"):
        write_plugin_patch(
            declaration, {"reduce_context_switch": True}, set(), expected_digest=revision
        )
    assert image.read_bytes() == saved
    with pytest.raises(PluginConfigChangedError, match="changed"):
        write_config_image(declaration, FleetConfig(), expected_digest=revision)
    values = json.loads(image.read_text())
    assert values["reduce_context_switch"] is True
    write_plugin_patch(declaration, {}, {"reduce_context_switch"}, expected_digest=None)
    assert json.loads(image.read_text())["reduce_context_switch"] is True
