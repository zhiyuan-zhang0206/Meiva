"""Permissions helper — the macOS desktop-automation daemon holding TCC grants.

`converge()` is the entry point for helper setup.
"""

from __future__ import annotations

from base.host.permissions_helper.client import HelperFactory
from services.desktop.permissions_helper.lifecycle import InstallEnv


# meiva:note
# Idempotent macOS helper bring-up: build, sign, and load.
#
# The door stays import-light: the lifecycle module loads on first use, so the test plugins
# that stub this function never import the configuration.
def converge(*, helper: HelperFactory, env: InstallEnv) -> None:
    from services.desktop.permissions_helper.lifecycle import converge as _converge

    _converge(helper=helper, env=env)
