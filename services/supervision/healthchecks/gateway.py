"""Read-only health probes for gateway; the root supervisor owns recovery."""

from collections.abc import Callable

from base.config import get_field
from base.daemon.health import DaemonProbe, probe_home


# meiva:note
# 2xx from the gateway health `url` AND a `home` matching this unit's `$MEIVA_HOME`,
# ALWAYS returning a verdict — see `base.daemon.health.probe_home`.
def gateway_probe_at(url: str) -> DaemonProbe:
    return probe_home(url)


# meiva:note
# The gateway verdict from the health URL the settings name when the probe runs.
def gateway_probe(*, read_url: Callable[[str], str] = get_field) -> DaemonProbe:
    return gateway_probe_at(str(read_url("gateway_health_url")))
