"""Permissions helper — the macOS desktop-automation daemon holding TCC grants.

`converge()` is the entry point for helper setup.
"""

from __future__ import annotations

from base.host.permissions_helper.client import HelperFactory
from services.desktop.permissions_helper.lifecycle import InstallEnv
from services.desktop.permissions_helper.lifecycle import converge as _converge


# meiva:note
# Idempotent macOS helper bring-up: build, sign, and load.
#
# The door loads the lifecycle module (and with it the build, signing and launchd modules) but no
# configuration: the helper port and the artifact directory arrive in `env`, built by the entry.
def converge(*, helper: HelperFactory, env: InstallEnv) -> None:
    _converge(helper=helper, env=env)
