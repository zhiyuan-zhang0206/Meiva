# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from base.clock import Timers
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import gateway_auth_headers
from base.cluster.machine.boot import gateway_access_from_boot
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machines import list_all
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.exit_codes import hard_exit
from base.host.system.hostport import Host
from base.host.system.probes import display_available
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import chrome_mcp_socket

from .session import BrowserRoutes


def browser_routes_from_boot(
    config: ConfigBoot,
    *,
    home: Path,
    timers: Timers,
    host: Host,
) -> BrowserRoutes:
    machine = ConfiguredMachineIdentity(config)
    local_machine: str | None = None
    with suppress(RuntimeError, OSError, ValueError):
        local_machine = machine.name()
    gate = ProcessDbGate(
        version=CodeVersion(LoadedCommit.capture()).get,
        process="chrome-routing",
        end_process=hard_exit,
    )

    def peers() -> dict[str, str | None]:
        db = Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )
        return dict(list_all(db))

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
