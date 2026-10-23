# meiva:note
# What a gateway process does at launch, decided from three facts: values in, plan out.
#
# `gateway.cluster.server.main` reads the facts once (does this machine serve the gateway
# role, does it declare a reachable address other machines can dial, is hot reload on) and
# executes the plan this module returns. The decisions that used to sit between its effects
# live here: whether the cluster secret is required, which address the listener binds, whether
# the transport-encryption declaration must hold, whether the cluster's minimum code version
# is raised, and whether the parent builds the ASGI app or leaves that to a reload worker.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GATEWAY_FACTORY", "ServePlan", "serve_plan"]

# meiva:note
# The import path a reload worker builds its own app from: the parent never constructs the
# app a worker will serve.
GATEWAY_FACTORY = "gateway.app:create_app_from_environment"

_LOOPBACK = "127.0.0.1"


# meiva:note
# What `main` executes, in order, for one gateway process.
@dataclass(frozen=True)
class ServePlan:
    # meiva:note
    # A gateway authenticates its API; a unit that serves the role refuses to start without
    # the human secret, before it touches the database or binds a port.
    require_cluster_secret: bool
    # meiva:note
    # "" = every interface, both address families (a wildcard socket per family). Not "::":
    # asyncio sets IPV6_V6ONLY on an explicit "::" bind, which refuses plain-IPv4 clients
    # (healthchecks, the SDK) outright.
    bind_host: str
    # meiva:note
    # Off the loopback the listener must run under a declared transport-encryption posture.
    transport_declaration_required: bool
    # meiva:note
    # A runner's local gateway holds no write on the row; only a gateway-serving unit raises
    # the cluster's minimum to its own code version, so an older process refuses to write.
    raise_min_code_version: bool
    # meiva:note
    # The factory path a reload worker builds its own app from, or None when this process
    # builds the app it serves.
    app_factory_path: str | None


# meiva:note
# The launch of one gateway process.
#
# A gateway-serving unit with a declared reachable address binds every interface so other
# machines can dial it. Everything else binds the loopback: the API is authenticated either
# way, but a box that declares no private-network address serves no other machine, and a
# runner's local gateway is dialed only by its local SDK and agent processes.
def serve_plan(*, serves_gateway: bool, off_box: bool, reload: bool) -> ServePlan:
    off_box_gateway = serves_gateway and off_box
    bind_host = "" if off_box_gateway else _LOOPBACK
    return ServePlan(
        require_cluster_secret=serves_gateway,
        bind_host=bind_host,
        transport_declaration_required=bind_host != _LOOPBACK,
        raise_min_code_version=serves_gateway,
        app_factory_path=GATEWAY_FACTORY if reload else None,
    )
