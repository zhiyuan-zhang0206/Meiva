"""`meiva.settings` exposes registered plugin configs as attributes."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.packages.plugin_config_images import PluginConfigOwner
from base.packages.plugins.config_face import disk_image_path
from base.packages.plugins.config_registration import (
    bind_plugin_config,
)
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation


class _FixtureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    flag: bool = Field(default=True)
    marker: str = Field(default=".git", json_schema_extra={"per_agent": True})


@pytest.fixture
def isolated_registry() -> Iterator[dict[str, BaseModel]]:
    """The SDK reads its current installation's own config bindings."""
    configs: dict[str, BaseModel] = {}
    with held_installation(Installation.bare(configs=configs, sampling=fixed_sampling_owner())):
        yield configs


def test_meiva_settings_plugins_attribute_access(
    isolated_registry: dict[str, BaseModel], unit_home: Path
):
    """`meiva.sdk_surface.settings.plugins.<n>` returns instance; unregistered plugin name raise + lists known plugins."""
    bind_plugin_config(
        PluginConfigOwner(
            "test_plugin", _FixtureConfig, disk_image_path("test_plugin", home=unit_home)
        ),
        isolated_registry,
    )

    import meiva.sdk_surface.settings as _meiva_settings

    cfg = _meiva_settings.plugins.test_plugin
    assert isinstance(cfg, _FixtureConfig)
    assert cfg.marker == ".git"

    with pytest.raises(AttributeError, match="Known plugins"):
        _ = _meiva_settings.plugins.nonexistent_plugin
