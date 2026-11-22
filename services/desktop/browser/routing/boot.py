# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from pydantic import TypeAdapter

from base.api_contracts.status import MachineStatus
from base.clock import Timers
from base.cluster.machine import gateway_api_base, gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.host.net.http_dial import get
from base.host.system.hostport import Host
from base.host.system.probes import display_available
from base.paths import chrome_mcp_socket

from .session import BrowserRoutes

_ROSTER = TypeAdapter(list[MachineStatus])


def browser_routes_from_boot(
    config: ConfigBoot,
    *,
    home: Path,
    timers: Timers,
    host: Host,
) -> BrowserRoutes:
    home = home.resolve()
    machine = ConfiguredMachineIdentity(config)
    local_machine: str | None = None
    with suppress(RuntimeError, OSError, ValueError):
        local_machine = machine.name()

    def peers() -> dict[str, str | None]:
        access = gateway_access_from_boot(config)
        url = gateway_api_base(access.gateway_url)
        response = get(f"{url}/api/cluster/roster", headers=gateway_auth_headers(access))
        response.raise_for_status()
        return {
            row.name: row.gateway_url or None for row in _ROSTER.validate_json(response.content)
        }

    socket = chrome_mcp_socket(home, config.view.services.browser_cdp_port)
    local_display = config.view.services.browser_enabled and display_available(host=host)
    return BrowserRoutes(
        home=home,
        local_machine=local_machine,
        local_available=lambda: local_display or socket.exists(),
        peers=peers,
        headers=lambda: gateway_auth_headers(gateway_access_from_boot(config)),
        timers=timers,
        connect_timeout_s=config.view.sandbox.mcp_connect_timeout_seconds,
    )
