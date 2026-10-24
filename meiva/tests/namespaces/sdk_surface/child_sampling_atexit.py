"""Child process for the sampling-owner atexit test: a refresh defect surfaces at shutdown."""

import meiva  # noqa: F401  # pyright: ignore[reportUnusedImport] (importing the SDK is under test)
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed

install.install(ExtensionRegistry(()), config_images=DiskPluginConfigImages(resolve_meiva_home()))
original = ValueError("ordinary SDK refresh defect")


def read() -> object:
    raise original


installation = installed()
assert installation is not None
owner = installation.require_sampling()
owner.fetch = read  # pyright: ignore[reportAttributeAccessIssue]
try:
    owner.read()
except ValueError as observed:
    assert observed is original
assert owner.worker is not None and owner.worker.completed.wait(2)
assert owner.error is not None and owner.error[0] is original
