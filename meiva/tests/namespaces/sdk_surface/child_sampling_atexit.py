"""Child process for the sampling-owner atexit test: a refresh defect surfaces at shutdown."""

import atexit
import contextlib
import sys
from pathlib import Path

import meiva  # noqa: F401  # pyright: ignore[reportUnusedImport] (importing the SDK is under test)
from base.agents.sdk.tests.sampling_owners import sampling_owner_failing_to_read
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed

# meiva:note
# The root of this process registers its sampling owner's close itself, as a bare SDK process does.
owner = sampling_owner_failing_to_read(Path(sys.argv[1]))
atexit.register(owner.close)
install.install(
    ExtensionRegistry(()),
    config_images=DiskPluginConfigImages(resolve_meiva_home()),
    sampling=owner,
)
installation = installed()
assert installation is not None and installation.require_sampling() is owner
with contextlib.suppress(UnicodeDecodeError):
    owner.read()
assert owner.worker is not None and owner.worker.completed.wait(2)
assert owner.error is not None and isinstance(owner.error[0], UnicodeDecodeError)
