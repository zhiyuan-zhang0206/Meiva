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

from base.cluster.machine.gateway_access import (
    GatewayAccess,
    GatewayApiBaseMissing,
    GatewayApiTokenMissing,
    gateway_api_base,
    gateway_bearer,
)
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
from base.host.net.auth import bearer_header, require_cluster_secret

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
    "roles_from_flags",
    "validate_machine_name",
]


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
