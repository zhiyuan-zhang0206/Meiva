# meiva:note
# Declares the cloudflared keepalive watchdog service for the host supervision roster.

from __future__ import annotations

from functools import partial
from pathlib import Path

from base.cluster.machine import MachineRole
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import DaemonProbe
from base.packages.plugins.config_registration import disk_image_path, read_config_image
from base.paths import run_dir
from meiva_builtins.plugins.meiva_cloudflared.default_config import CloudflaredConfig
from services.supervision.healthchecks.protocol_probe import ping
from services.supervision.roster.service_spec import ServiceSpec

_BOTH: frozenset[MachineRole] = frozenset({"gateway", "agent-runner"})


def watchdog_pidfile_path(home: Path) -> Path:
    return run_dir(home) / "cloudflared.pid"


def watchdog_socket_path(home: Path) -> Path:
    return run_dir(home) / "cloudflared-watchdog.sock"


def probe_cloudflared(home: Path) -> DaemonProbe:
    socket_path = watchdog_socket_path(home)
    try:
        return ping(socket_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"cloudflared watchdog socket probe failed: {exc}")


def _cloudflared_gate(config: CloudflaredConfig) -> str | None:
    if not config.cloudflared_enabled:
        return "disabled (meiva_cloudflared.cloudflared_enabled off)"
    return None


def services(*, home: Path, endpoints: ServiceEndpoints) -> tuple[ServiceSpec, ...]:
    _ = endpoints
    config_path = disk_image_path("meiva_cloudflared", home=home)
    config = read_config_image(CloudflaredConfig, config_path)
    return (
        ServiceSpec(
            session="cloudflared",
            cmd=".venv/bin/python -m meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon",
            capabilities=_BOTH,
            requires_db=False,
            pidfile=watchdog_pidfile_path(home),
            identity_probe=partial(probe_cloudflared, home=home),
            gate=partial(_cloudflared_gate, config),
            plugin_config=("meiva_cloudflared", config),
            config_inputs=(config_path,),
        ),
    )
