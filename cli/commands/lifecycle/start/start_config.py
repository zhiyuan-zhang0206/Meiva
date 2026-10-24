# meiva:note
# The configuration values one `meiva start` reads, read once and handed down.
#
# The start entry is a composition root: it reads these fields from the invocation's own
# configuration owner after the unit's identity resolved, and every phase below it receives the
# result. A test hands a phase the exact values it wants with `dataclasses.replace`.

from __future__ import annotations

from dataclasses import dataclass

from base.config import ConfigBoot
from cli.commands.data_plane.config import DataPlaneConfig, data_plane_config_from_boot

__all__ = ["StartConfig", "read_start_config"]


# meiva:note
# The `general`, `gateway`, `services` and data-plane fields a start reads. The WAL-G configuration
# file is one of the data plane's (`data_plane.walg_config_file`) and is carried nowhere else.
@dataclass(frozen=True)
class StartConfig:
    service_path: str
    data_plane: DataPlaneConfig
    gateway_url: str
    gateway_port: int
    browser_origin: str
    entry_url: str
    app_port: int | None
    browser_cdp_port: int


# meiva:note
# The values this start reads, taken from `config` now.
def read_start_config(config: ConfigBoot) -> StartConfig:
    view = config.view
    return StartConfig(
        service_path=view.general.service_path,
        data_plane=data_plane_config_from_boot(config),
        gateway_url=view.gateway.gateway_url,
        gateway_port=view.gateway.gateway_port,
        browser_origin=view.gateway.browser_origin,
        entry_url=view.services.frontend_healthcheck_url,
        app_port=view.services.app_port,
        browser_cdp_port=view.services.browser_cdp_port,
    )
