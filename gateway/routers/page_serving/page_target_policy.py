# meiva:note
# Which hosts and ports a page may be registered on and proxied to: values in, refusal out.
#
# The gateway dials `http://{host}:{port}/...` for any registered page, so a compromised agent
# must not point the proxy at arbitrary addresses (another machine's services, or the cluster's
# own Postgres / Redis / IM bridge / agent-ops: a standing internal-network probe). A page may
# name a loopback host (the single-box posture) or an address its agent's home machine
# advertises; a port below 1024 is the system's own. `page_target.py` reads the registry; the
# decisions are here.

from __future__ import annotations

import ipaddress
from collections.abc import Collection
from dataclasses import dataclass

from gateway.routers.refusal import HttpFailure

__all__ = [
    "ProxyDial",
    "is_loopback_host",
    "nonloopback_refusal",
    "privileged_port_refusal",
    "proxy_dial",
]

_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


# meiva:note
# How the gateway dials a page server.
@dataclass(frozen=True)
class ProxyDial:
    connect_s: float
    read_s: float
    write_s: float
    pool_s: float
    trust_env: bool


# meiva:note
# The dial parameters for one page-proxy request.
#
# The dial target is a registry-validated cluster host, so the client never routes it
# through the machine's ambient HTTP proxy (a proxy-configured gateway host once forwarded
# its proxy's own 502, 2026-09-10): `trust_env` is False. `read_s` bounds the time between
# chunks, so a live SSE stream or a slow large download keeps flowing while a stalled
# backend is severed.
def proxy_dial(connect_s: float, read_s: float) -> ProxyDial:
    return ProxyDial(connect_s=connect_s, read_s=read_s, write_s=5.0, pool_s=5.0, trust_env=False)


# meiva:note
# Whether `host` names the local machine, in any loopback spelling.
#
# The literal names plus an `ipaddress` check cover 127.0.0.0/8, ::1 and the IPv4-mapped
# ::ffff:127.x forms.
def is_loopback_host(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# meiva:note
# A page server is an unprivileged user-space HTTP server; ports below 1024 are the
# system's own services.
def privileged_port_refusal(port: int) -> HttpFailure | None:
    if port < 1024:
        return HttpFailure(
            400, f"page port {port} is privileged — page servers run on ports >= 1024"
        )
    return None


# meiva:note
# Fail closed unless `host` is an address the agent's home machine advertises.
#
# `machine` is the agent's registered home machine (None when unknown); the hosts are its
# name plus every address its running units advertise.
def nonloopback_refusal(
    host: str,
    agent_id: int,
    machine: str | None,
    advertised_hosts: Collection[str],
    *,
    status_code: int,
) -> HttpFailure | None:
    if machine is None:
        return HttpFailure(
            status_code,
            f"page host {host!r} is not loopback and agent {agent_id}'s home "
            "machine is unknown — refusing to proxy to it",
        )
    if host not in advertised_hosts:
        return HttpFailure(
            status_code,
            f"page host {host!r} is neither loopback nor {machine!r}'s "
            "registered address — refusing to proxy to it",
        )
    return None
