import sys
from types import SimpleNamespace

from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkNamespace,
)
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.tests.installation_holder import inert_sampling
from tests.fixtures.plugin_registrations import plugin_registrations_present

assert "agent.state" not in sys.modules, "the fresh interpreter must start without the agent layer"
assert not plugin_registrations_present()
install.install(
    ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_namespaces=(SdkNamespace("gate_probe", SimpleNamespace()),)
                ),
            ),
        )
    ),
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=inert_sampling(),
)
assert plugin_registrations_present()
assert "agent.state" not in sys.modules, "asking must not load the agent layer"
