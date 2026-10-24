# meiva:note
# How a caller reaches and authenticates to the gateway API, as a value.
#
# `GatewayAccess` is built once by a composition root from its own configuration
# (`gateway_access_from_boot`): the gateway URL, the human cluster secret, the facts about
# the data plane and the launch environment that decide which credential this process
# presents, and the machine API token it was launched with. Everything below is pure over
# that value — the credential policy never reads the environment, a file or Settings.


from __future__ import annotations

from dataclasses import dataclass, field

from base.host.env.process_profile import ProcessProfile

__all__ = [
    "GatewayAccess",
    "GatewayApiBaseMissing",
    "GatewayApiTokenMissing",
    "gateway_api_base",
    "gateway_bearer",
]


# meiva:note
# gateway_url unset — cannot resolve where to reach the gateway.
class GatewayApiBaseMissing(RuntimeError):  # noqa: N818
    ...


# meiva:note
# An agent- or runner-profile process has no machine API token although the cluster API is authenticated.
class GatewayApiTokenMissing(RuntimeError):  # noqa: N818
    ...


def _normalize_gateway_url(value: str) -> str | None:
    value = value.strip()
    return value.rstrip("/") if value else None


# meiva:note
# Base URL this unit uses to call the gateway over HTTP.
#
# One role-blind resolver for every client-side caller (the agent SDK, the
# monitor wrapper, the runner, the watchdog, and dev bootstrap): the configured
# gateway address (`MEIVA_GATEWAY_URL`), normalized, else raise. A gateway-capable host
# resolves the *same* configured address as any other caller — on a single box that
# address is the box's own reachable address, so its agent-runner half dials its
# gateway half the same way a remote runner would. No localhost shortcut: single-box
# and split deployments share one path.
#
# Raises:
#     GatewayApiBaseMissing: `gateway_url` is unset.
def gateway_api_base(gateway_url: str) -> str:
    url = _normalize_gateway_url(gateway_url)
    if url is None:
        raise GatewayApiBaseMissing(
            "gateway_url unset — `meiva start` writes it on an agent-runner; "
            "or `export MEIVA_GATEWAY_URL=<gateway url>` (a gateway host sets its "
            "own reachable URL there too)."
        )
    return url


# meiva:note
# What a client needs to reach and authenticate to the gateway API.
#
# Built once by the composition root from its own configuration (`gateway_access_from_boot`
# from a `ConfigBoot`, which reads the launch environment the boot owns); the secrets never
# reach a repr.
#
# - `remote_plane`: the data plane is remote-managed (keeps no write generations, so no
#   machine API token is issued; see `gateway_bearer`).
# - `machine_token`: the machine API token of the active write generation this process was
#   launched with (`MEIVA_API_TOKEN`), delivered by the launcher or by the boot pass to an
#   admitted operator process; None when it holds none.
# - `launcher_profile`: the process profile of the launcher tree this process belongs to,
#   live or recorded (`base.host.env.bootstrap.launcher_context`); None for a plain shell.
# - `gateway_home`: this unit serves the gateway, so its config source is its own `.env`
#   (`base.host.env.bootstrap.config_source_is_local`).
@dataclass(frozen=True)
class GatewayAccess:
    gateway_url: str
    cluster_secret: str = field(repr=False)
    remote_plane: bool
    machine_token: str | None = field(repr=False)
    launcher_profile: ProcessProfile | None
    gateway_home: bool


# meiva:note
# Launcher profiles whose processes present the machine API token and never the
# human secret: the launcher delivers one to every agent host and runner service
# (`cli.commands.data_plane.bringup.api_delivery`), exec children inherit it.
_TOKEN_ONLY_PROFILES: frozenset[ProcessProfile] = frozenset({"agent", "runner"})


# meiva:note
# The bearer this process presents to the gateway API.
#
# The machine API token of the active write generation first: delivered by
# the launcher, or by the boot pass to an admitted operator process. Without
# one, the human cluster secret (an operator on the gateway home; a
# gateway-profile service) — except in an agent- or runner-profile process,
# which holds its token whenever the cluster API is authenticated, so a
# missing token there is a launch defect and raises instead of silently
# presenting the human secret. A remote-managed data plane keeps no write
# generations and delivers no token (`api_delivery`): its gateway-local
# services present the human secret, so that plane is exempt.
#
# Raises:
#     GatewayApiTokenMissing: an agent- or runner-profile process has no
#         `MEIVA_API_TOKEN`, or the process holds neither a token nor the
#         human secret (the API always authenticates).
def gateway_bearer(access: GatewayAccess) -> str:
    if access.machine_token:
        return access.machine_token
    profile = access.launcher_profile
    plane_delivers_no_tokens = access.gateway_home and access.remote_plane
    if profile in _TOKEN_ONLY_PROFILES and not plane_delivers_no_tokens:
        raise GatewayApiTokenMissing(
            f"this {profile}-profile process carries no machine API token (MEIVA_API_TOKEN) "
            "while the cluster API is authenticated. The root "
            "launcher delivers the active write generation's token to every agent host and "
            "runner service it starts, and such a process never presents the human secret "
            "instead: restart the cluster's services (stop with `meiva stop`, then start again: see `meiva stop --help` and "
            "`meiva start --help`) so the launcher delivers it."
        )
    if not access.cluster_secret:
        raise GatewayApiTokenMissing(
            "this process holds no gateway credential: no machine API token (MEIVA_API_TOKEN) "
            "and no MEIVA_CLUSTER_SECRET, and the cluster API always authenticates. Run it from "
            "the gateway home, or from a unit whose database capability is installed"
        )
    return access.cluster_secret
