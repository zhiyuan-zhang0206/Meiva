from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed

install.install(ExtensionRegistry(()), config_images=DiskPluginConfigImages(resolve_meiva_home()))
installation = installed()
assert installation is not None
install.uninstall()
installation.sampling.error = (
    ValueError("a removed surface must not close at interpreter exit"),
    None,
)
print("uninstalled")
