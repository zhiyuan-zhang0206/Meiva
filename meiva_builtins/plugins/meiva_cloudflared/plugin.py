# meiva:note
# Builtin plugin entrypoint for meiva_cloudflared; host service is declared via services.py.

from __future__ import annotations

from base.packages.plugins.extensions import PluginContributions

__description__ = "Cloudflared tunnel keepalive service"


def contribute() -> PluginContributions:
    return PluginContributions()
