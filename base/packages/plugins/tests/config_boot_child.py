"""Synchronize two real config-image creators before their locked CAS."""

import sys

from pydantic import BaseModel

from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_config_images import PluginConfigOwner
from base.packages.plugins.config_registration import (
    bind_plugin_config,
    disk_image_path,
    get_plugin_config,
)


class Config(BaseModel):
    marker: str = sys.argv[1]


class SynchronizedOwner(PluginConfigOwner):
    def create_default(self) -> None:
        print("READY", flush=True)
        assert sys.stdin.readline() == "CREATE\n"
        super().create_default()


configs: dict[str, BaseModel] = {}
bind_plugin_config(SynchronizedOwner("boot-race", Config, disk_image_path("boot-race")), configs)
slices = AgentSlices.resolve(
    plugin_configs=configs,
    default_reader=lambda domain, field: getattr(getattr(settings, domain), field),
)
print(get_plugin_config("boot-race", slices, Config).marker, flush=True)
