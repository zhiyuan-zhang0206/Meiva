# meiva:note
# What the gate process serves, decided once: values in, plan out.
#
# The process root reads the command line and the settings, then hands this module the facts.
# It answers the port to bind, the static pages directory, the app origin to proxy and the
# gateway origin the login page posts to.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["GateFacts", "GateLaunch", "app_port_of", "gateway_base", "plan_launch"]


# meiva:note
# What the process root read: the settings and the machine's reachable host.
@dataclass(frozen=True)
class GateFacts:
    entry_port: int
    app_port: int
    gateway_port: int
    gateway_host: str
    browser_origin: str
    default_static_dir: Path


# meiva:note
# What the gate binds and proxies.
@dataclass(frozen=True)
class GateLaunch:
    port: int
    static_dir: Path
    app_base: str
    gateway_base: str
    browser_origin: str


# meiva:note
# The app's port: the configured one, else the slot after the entry port.
def app_port_of(configured: int | None, *, entry_port: int) -> int:
    return configured or entry_port + 1


# meiva:note
# The gateway API base the gate probes and the login page posts to.
#
# The host must be one the user's browser can dial: the login page posts the cluster secret
# to this origin, and a remote browser resolving `127.0.0.1` would hit itself. The host is the
# machine's declared owner host (`MEIVA_MACHINE_HOST`, else `localhost`), the same operator
# address `meiva.ui.show` hands the user for direct page URLs. The server-side auth probe
# follows the same base: on a gateway host the gateway binds all interfaces, so dialing the
# host's own reachable address is equivalent to loopback and keeps one origin everywhere.
def gateway_base(*, host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


# meiva:note
# The launch of one gate process. An explicit `--port` or `--static-dir` wins over the
# settings and the checked-in pages; the app is always the loopback listener on its slot.
def plan_launch(*, port: int | None, static_dir: Path | None, facts: GateFacts) -> GateLaunch:
    return GateLaunch(
        port=port or facts.entry_port,
        static_dir=static_dir or facts.default_static_dir,
        app_base=f"http://127.0.0.1:{facts.app_port}",
        gateway_base=gateway_base(host=facts.gateway_host, port=facts.gateway_port),
        browser_origin=facts.browser_origin,
    )
