# meiva:note
# Real process shutdown after installing, transferring or failing to replace an SDK surface.
import atexit
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import meiva  # noqa: F401  # pyright: ignore[reportUnusedImport] (importing the SDK is under test)
from base.agents.sdk import call_policy
from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.agents.sdk.tests.sampling_owners import sampling_owner_failing_to_read
from base.clock.tests.seeded_rng import seeded_rng
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins import enable_config
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.boot import process_sampling_owner
from meiva.sdk_surface import install
from meiva.sdk_surface.extensions import load_extensions
from meiva.sdk_surface.installation import installed


class Mode(StrEnum):
    INSTALL = "install"
    FAILURE = "failure"
    RELOAD = "reload"
    RELOAD_EXPLICIT = "reload-explicit"
    SERVICE_RELOAD = "service-reload"
    FAILED_RELOAD = "failed-reload"


owners: list[SamplingPolicyOwner] = []


# meiva:note
# Registered before the root builds an owner, so exit reports its final state after its close.
def report_owner() -> None:
    print("closed" if owners[0].stopped else "open")


atexit.register(report_owner)
mode = Mode(sys.argv[1]) if len(sys.argv) > 1 else Mode.INSTALL
match mode:
    case Mode.FAILURE:
        owner = sampling_owner_failing_to_read(Path(sys.argv[2]))
        atexit.register(owner.close)
        owner.refresh()
    case Mode.SERVICE_RELOAD:
        owner = call_policy.process_sampling_owner(seeded_rng())
    case Mode.INSTALL | Mode.RELOAD | Mode.RELOAD_EXPLICIT | Mode.FAILED_RELOAD:
        owner = process_sampling_owner(seeded_rng())
    case _:
        assert_never(mode)
owners.append(owner)
install.install(
    ExtensionRegistry(()),
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=owner,
)
if mode in {Mode.RELOAD, Mode.RELOAD_EXPLICIT, Mode.SERVICE_RELOAD, Mode.FAILED_RELOAD}:
    enable_config.write_local(
        {"plugins": {name: {"enabled": False} for name in enable_config.discover_plugins()}}
    )
    if mode is Mode.FAILED_RELOAD:
        enable_config.local_config_path().write_text("{")
        try:
            load_extensions()
        except json.JSONDecodeError:
            assert installed() is None
        else:
            raise AssertionError("the malformed plugin config did not fail the full reload")
    elif mode is Mode.RELOAD_EXPLICIT:
        load_extensions(sampling=owner)
    else:
        load_extensions()
    if mode is not Mode.FAILED_RELOAD:
        replacement = installed()
        assert replacement is not None and replacement.sampling is owner
