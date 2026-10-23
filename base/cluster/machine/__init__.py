"""Explicitly owned machine identity and cluster capability values.

Composition roots supply four independent lazy readers. Each MachineIdentity
caches its own name, capabilities, reachable host and optional description;
resetting one owner never changes another. The startup factory is defined in
base.cluster.machine.boot and consumes the root's ConfigBoot without reading
any fields at construction. SDK consumers use the machine in their MeivaContext.

Machine names are stable deployment labels, never inferred from hostnames.
Capabilities are independently declared gateway, agent-runner and
observability-station flags; a host runs their union. Missing name/role and
invalid capability tokens retain their explicit errors. Gateway URL and bearer
helpers validate the existing client credential policy independently of identity.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from base.cluster.machine.identity import (
    MachineIdentity,
    MachineNameMissing,
    MachineRole,
    MachineRoleInvalid,
    MachineRoleMissing,
    MachineRoles,
    format_capabilities,
    roles_from_flags,
    validate_machine_name,
)
from base.host.env.dotenv_boot import launcher_context
from base.host.net.auth import bearer_header, delivered_token, require_cluster_secret

__all__ = [
    "GatewayAccess",
    "GatewayApiBaseMissing",
    "GatewayApiTokenMissing",
    "MachineIdentity",
    "MachineNameMissing",
    "MachineRole",
    "MachineRoleInvalid",
    "MachineRoleMissing",
    "MachineRoles",
    "daemon_acceptance",
    "format_capabilities",
    "gateway_api_base",
    "gateway_auth_headers",
    "gateway_bearer",
    "resolve_gateway_bearer",
    "roles_from_flags",
    "validate_machine_name",
]


class GatewayApiBaseMissing(RuntimeError):  # noqa: N818 — state description, same style as MachineNameMissing
    """gateway_url unset — cannot resolve where to reach the gateway."""


class GatewayApiTokenMissing(RuntimeError):  # noqa: N818 — state description, same style as GatewayApiBaseMissing
    """An agent- or runner-profile process has no machine API token although the cluster API is authenticated."""


def _normalize_gateway_url(value: str) -> str | None:
    value = value.strip()
    return value.rstrip("/") if value else None


def gateway_api_base(gateway_url: str) -> str:
    """Base URL this unit uses to call the gateway over HTTP.

    One role-blind resolver for every client-side caller (the agent SDK, the
    monitor wrapper, the runner, the watchdog, and dev bootstrap): the configured
    gateway address (`MEIVA_GATEWAY_URL`), normalized, else raise. A gateway-capable host
    resolves the *same* configured address as any other caller — on a single box that
    address is the box's own reachable address, so its agent-runner half dials its
    gateway half the same way a remote runner would. No localhost shortcut: single-box
    and split deployments share one path.

    Raises:
        GatewayApiBaseMissing: `gateway_url` is unset.
    """
    url = _normalize_gateway_url(gateway_url)
    if url is None:
        raise GatewayApiBaseMissing(
            "gateway_url unset — `meiva start` writes it on an agent-runner; "
            "or `export MEIVA_GATEWAY_URL=<gateway url>` (a gateway host sets its "
            "own reachable URL there too)."
        )
    return url


@dataclass(frozen=True)
class GatewayAccess:
    """What a client needs to reach and authenticate to the gateway API.

    Built once by the composition root from its own configuration (`gateway_access_from_boot`);
    the cluster secret never reaches a repr. `remote_plane`: the data plane is remote-managed
    (keeps no write generations, so no machine API token is issued; see `gateway_bearer`).
    """

    gateway_url: str
    cluster_secret: str = field(repr=False)
    remote_plane: bool


# Launcher profiles whose processes present the machine API token and never the
# human secret: the launcher delivers one to every agent host and runner service
# (`cli.commands.data_plane.bringup.api_delivery`), exec children inherit it.
_TOKEN_ONLY_PROFILES = frozenset({"agent", "runner"})


def gateway_bearer(access: GatewayAccess) -> str:
    """The bearer this process presents to the gateway API.

    The machine API token of the active write generation first: delivered by
    the launcher, or by the boot pass to an admitted operator process. Without
    one, the human cluster secret (an operator on the gateway home; a
    gateway-profile service) — except in an agent- or runner-profile process,
    which holds its token whenever the cluster API is authenticated, so a
    missing token there is a launch defect and raises instead of silently
    presenting the human secret. A remote-managed data plane keeps no write
    generations and delivers no token (`api_delivery`): its gateway-local
    services present the human secret, so that plane is exempt.

    Raises:
        GatewayApiTokenMissing: an agent- or runner-profile process has no
            `MEIVA_API_TOKEN`, or the process holds neither a token nor the
            human secret (the API always authenticates).
    """
    return resolve_gateway_bearer(
        token_reader=delivered_token,
        secret_reader=lambda: access.cluster_secret,
        profile_reader=launcher_context,
        no_tokens_reader=lambda: _plane_delivers_no_tokens(access),
    )


def resolve_gateway_bearer(
    *,
    token_reader: Callable[[], str | None],
    secret_reader: Callable[[], str],
    profile_reader: Callable[[], str | None],
    no_tokens_reader: Callable[[], bool],
) -> str:
    """Read one owner's credential policy, preserving token-first lazy admission."""
    token = token_reader()
    if token:
        return token
    secret = secret_reader()
    profile = profile_reader()
    if profile in _TOKEN_ONLY_PROFILES and not no_tokens_reader():
        raise GatewayApiTokenMissing(
            f"this {profile}-profile process carries no machine API token (MEIVA_API_TOKEN) "
            "while the cluster API is authenticated. The root "
            "launcher delivers the active write generation's token to every agent host and "
            "runner service it starts, and such a process never presents the human secret "
            "instead: restart the cluster's services (stop with `meiva stop`, then start again: see `meiva stop --help` and "
            "`meiva start --help`) so the launcher delivers it."
        )
    if not secret:
        raise GatewayApiTokenMissing(
            "this process holds no gateway credential: no machine API token (MEIVA_API_TOKEN) "
            "and no MEIVA_CLUSTER_SECRET, and the cluster API always authenticates. Run it from "
            "the gateway home, or from a unit whose database capability is installed"
        )
    return secret


def _plane_delivers_no_tokens(access: GatewayAccess) -> bool:
    """Whether this unit is a gateway home on a remote-managed data plane.

    The one case where `api_delivery` hands out no token although the API is
    authenticated: the plane keeps no write generations to issue one from.
    """
    from base.host.env.bootstrap import config_source_is_local

    return config_source_is_local() and access.remote_plane


def daemon_acceptance(access: GatewayAccess) -> frozenset[str]:
    """SHA-256 digests of the bearers a gateway-side daemon's authenticated endpoint accepts.
    The acceptor's half of `gateway_bearer(access)`: the daemons that
    take calls from the gateway (the ops server, im_bridge's `/send`) accept machine API tokens,
    never the human cluster secret.

    The gateway dials with its gateway-class token and a runner-class process of the unit with its
    runner token, so a daemon accepts exactly its write generation's two tokens. A remote unit
    takes the gateway token digest its installed capability carries plus its own runner token's:
    it never holds the gateway token or the human secret. The gateway home takes the active
    generation's two tokens (none while no generation is active: fail closed).

    The one place the human secret is still accepted is a remote-managed data plane
    (`access.remote_plane`): it keeps no write generations and issues no token, so
    its gateway-local services present, and its daemons accept, the human secret. That branch
    is the exception to "outside the gateway, the CLI and the root, no component holds the
    cluster secret" (docs/decisions/data/security/2026-10-03-cluster-secret-contraction.md); the live
    deployment's data plane is local, and this branch only runs when `is_remote`.
    """
    from base.host.db_authority.api import acceptance, token_digest
    from base.host.db_authority.unit import load_unit_capability
    from base.paths import meiva_home

    home = meiva_home().resolve()
    capability = load_unit_capability(home)
    if capability is not None:
        api = capability.api
        return frozenset({api.gateway, token_digest(api.token)})
    secret = require_cluster_secret(access.cluster_secret, where=f"home {home}")
    if access.remote_plane:
        return frozenset({token_digest(secret)})
    return frozenset(acceptance(home).values())


def gateway_auth_headers(access: GatewayAccess) -> dict[str, str]:
    """Auth headers a client presents to the cluster's authenticated surfaces.

    The Bearer paired with `gateway_api_base(access.gateway_url)` for every client-side call to an
    authenticated gateway route (`/api/cluster/*`, `/api/agents/*`,
    `/api/config`, `/api/memory/*`, ...) and with the gateway's `/ops` dials:
    `gateway_bearer(access)`.

    Raises:
        GatewayApiTokenMissing: see `gateway_bearer`.
    """
    return bearer_header(gateway_bearer(access))
