# meiva:note
# What a remote unit's join refuses, decided from the values it was given.
#
# `meiva init` and `meiva cluster db-authority install-unit` hand the stored identity values
# to the join; these checks fail fast on a unit that could never reach its gateway or that
# holds a credential it must never hold, before anything is fetched or written. They also
# read what the gateway served: a join never trusts a payload it has not checked.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.host.net.predicates import is_loopback_host

__all__ = [
    "JoinTarget",
    "data_plane_urls",
    "join_target",
    "require_routable_data_plane",
    "served_value",
]


# meiva:note
# The gateway a unit joins, and whether it is reached over the network (a loopback gateway
# is the unit's own box, where nothing needs to be routable).
@dataclass(frozen=True)
class JoinTarget:
    gateway: str
    remote: bool


# meiva:note
# The unit never holds the human cluster secret: it authenticates with the machine API token
# of its capability. A remote gateway also needs a reachable `--machine-host`, or the gateway
# could not dial the unit back. `gateway_host` is the host name the shell parsed out of the
# gateway URL (empty when the URL has none); it decides whether the gateway is remote.
def join_target(values: Mapping[str, str], home: str, gateway_host: str) -> JoinTarget:
    if "MEIVA_CLUSTER_SECRET" in values:
        raise ValueError(
            f"this remote unit's home ({home}) records the human cluster secret; a remote "
            "unit authenticates with its capability's machine API token and never holds "
            "it. Remove MEIVA_CLUSTER_SECRET from its .env"
        )
    gateway = values["MEIVA_GATEWAY_URL"]
    machine_host = values.get("MEIVA_MACHINE_HOST", "")
    remote = not is_loopback_host(gateway_host)
    if remote and (not machine_host or is_loopback_host(machine_host)):
        raise ValueError("joining a remote gateway requires a reachable --machine-host")
    return JoinTarget(gateway, remote)


# meiva:note
# One value of the gateway's bootstrap payload, or a refusal naming the key it lacks: a
# gateway that serves a different shape than this unit expects is a version skew, and the
# join stops there instead of failing on a missing key later.
def served_value(served: Mapping[str, str], key: str) -> str:
    try:
        return served[key]
    except KeyError:
        raise ValueError(f"the gateway's bootstrap serves no {key}") from None


# meiva:note
# The database and Redis URLs the gateway serves, in that order, for the shell to read the
# host names out of.
def data_plane_urls(served: Mapping[str, str]) -> tuple[str, str]:
    return served_value(served, "MEIVA_DB_URL"), served_value(served, "MEIVA_REDIS_URL")


# meiva:note
# A remote gateway that serves loopback data-plane URLs describes its own box, which the
# unit cannot reach. `data_plane_hosts` are the host names of the served database and Redis
# URLs.
def require_routable_data_plane(target: JoinTarget, data_plane_hosts: Sequence[str]) -> None:
    if target.remote and any(is_loopback_host(host) for host in data_plane_hosts):
        raise ValueError("remote gateway returned loopback data-plane URLs")
