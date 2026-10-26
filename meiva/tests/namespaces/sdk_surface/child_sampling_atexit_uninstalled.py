import atexit

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.clock.tests.seeded_rng import seeded_rng
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.boot import process_sampling_owner
from meiva.sdk_surface import install

owners: list[SamplingPolicyOwner] = []


# meiva:note
# Registered before the owner exists, so interpreter exit runs it after any close the owner has.
def report_owner() -> None:
    print("closed" if owners[0].stopped else "open")


atexit.register(report_owner)
owners.append(process_sampling_owner(seeded_rng()))
install.install(
    ExtensionRegistry(()),
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=owners[0],
)
install.uninstall()
