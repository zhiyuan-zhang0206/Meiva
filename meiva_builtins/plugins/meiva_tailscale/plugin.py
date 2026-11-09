# meiva:note
# Builtin plugin entrypoint for meiva_tailscale; host service is declared via services.py.

from __future__ import annotations

from base.packages.plugins.extensions import PluginContributions

__description__ = "Tailscale mesh VPN keepalive service"


def contribute() -> PluginContributions:
    return PluginContributions()
