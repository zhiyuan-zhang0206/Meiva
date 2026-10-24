import atexit

from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed

# meiva:note
# The root of this process registers its sampling owner's close itself, as a bare SDK process does.
owner = fixed_sampling_owner()
atexit.register(owner.close)
install.install(
    ExtensionRegistry(()),
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=owner,
)
installation = installed()
assert installation is not None
install.uninstall()
installation.require_sampling().error = (
    ValueError("a removed surface must not close at interpreter exit"),
    None,
)
print("uninstalled")
