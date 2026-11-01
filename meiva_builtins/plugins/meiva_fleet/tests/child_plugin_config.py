# meiva:note
# Reads the fleet plugin configuration the way a booted unit composes it and prints the outcome as
# one JSON line: the configuration, or the class and message of the refusal. A test fixes the unit's
# role and the packets delivered to it through this process's real environment.
import json
import os
import sys

from base.packages.plugins.config_registration import (
    disk_image_path,
    read_plugin_config,
    read_service_config,
)
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from base.paths import meiva_home
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

mode = sys.argv[1]
if mode not in ("image", "service"):
    raise ValueError(f"unknown mode {mode!r}")
try:
    config = (
        read_plugin_config(
            "meiva_fleet", FleetConfig, disk_image_path("meiva_fleet", home=meiva_home())
        )
        if mode == "image"
        else read_service_config("meiva_fleet", FleetConfig, environ=os.environ)
    )
except (InvalidConfigData, SchemaDriftError, TypeError) as refusal:
    print(json.dumps({"refused": type(refusal).__name__, "message": str(refusal)}))
else:
    print(json.dumps({"config": None if config is None else config.model_dump(mode="json")}))
