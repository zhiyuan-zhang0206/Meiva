# meiva:note
# Declares the Tailscale keepalive watchdog service for the host supervision roster.

from __future__ import annotations

from functools import partial
from pathlib import Path

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe
from base.packages.plugins.config_registration import disk_image_path, read_config_image
from base.paths import run_dir
from meiva_builtins.plugins.meiva_tailscale.default_config import TailscaleConfig
from services.supervision.healthchecks.protocol_probe import ping
from services.supervision.roster.service_spec import ServiceSpec

_BOTH: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})


def watchdog_pidfile_path(home: Path) -> Path:
    return run_dir(home) / "tailscale.pid"


def watchdog_socket_path(home: Path) -> Path:
    return run_dir(home) / "tailscale-watchdog.sock"


def probe_tailscale(home: Path) -> DaemonProbe:
    socket_path = watchdog_socket_path(home)
    try:
        return ping(socket_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"tailscale watchdog socket probe failed: {exc}")


def _tailscale_gate(config: TailscaleConfig) -> str | None:
    if not config.tailscale_enabled:
        return "disabled (meiva_tailscale.tailscale_enabled off)"
    return None


def services(*, home: Path, endpoints: ServiceEndpoints) -> tuple[ServiceSpec, ...]:
    _ = endpoints
    config_path = disk_image_path("meiva_tailscale", home=home)
    config = read_config_image(TailscaleConfig, config_path)
    return (
        ServiceSpec(
            session="tailscale",
            cmd=".venv/bin/python -m meiva_builtins.plugins.meiva_tailscale.service_declaration.daemon",
            capabilities=_BOTH,
            requires_db=False,
            pidfile=watchdog_pidfile_path(home),
            identity_probe=partial(probe_tailscale, home=home),
            gate=partial(_tailscale_gate, config),
            plugin_config=("meiva_tailscale", config),
            config_inputs=(config_path,),
        ),
    )
