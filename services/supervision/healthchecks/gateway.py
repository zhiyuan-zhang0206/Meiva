"""Read-only health probes for gateway; the root supervisor owns recovery."""

from pathlib import Path

from base.daemon.health import DaemonProbe, probe_home


# meiva:note
# 2xx from the gateway health `url` AND a `home` matching the unit home the roster
# hands in, ALWAYS returning a verdict — see `base.daemon.health.probe_home`.
def gateway_probe_at(url: str, *, home: Path) -> DaemonProbe:
    return probe_home(url, expected_home=home)
