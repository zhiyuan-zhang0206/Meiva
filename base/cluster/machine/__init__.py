# meiva:note
# Explicitly owned machine identity and cluster capability values.
#
# Composition roots construct `ConfiguredMachineIdentity(config)` in
# `base.cluster.machine.configured` without reading any fields at construction.
# Each owner caches its own name, capabilities, reachable host and optional
# description; resetting one owner never changes another. Read-only consumers take
# `MachineIdentityReader`, which both `ConfiguredMachineIdentity` and
# `MachineFacts` satisfy. SDK consumers use the machine in their `MeivaContext`.
#
# Machine names are stable deployment labels, never inferred from hostnames.
# Capabilities are independently declared gateway, agent-runner and
# observability-station flags; a host runs their union. Missing name/role and
# invalid capability tokens retain their explicit errors. Gateway URL and bearer
# helpers validate the existing client credential policy independently of identity.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from base.cluster.machine.gateway_access import (
    GatewayAccess,
    GatewayApiBaseMissing,
    GatewayApiTokenMissing,
    gateway_api_base,
    gateway_bearer,
)
from base.cluster.machine.identity import (
    MachineFacts,
    MachineIdentityReader,
    MachineNameMissing,
    MachineRole,
    MachineRoleInvalid,
    MachineRoleMissing,
    MachineRoles,
    format_capabilities,
    roles_from_flags,
    validate_machine_name,
)
from base.host.net import bearer_header

__all__ = [
    "GatewayAccess",
    "GatewayApiBaseMissing",
    "GatewayApiTokenMissing",
    "MachineFacts",
    "MachineIdentityReader",
    "MachineNameMissing",
    "MachineRole",
    "MachineRoleInvalid",
    "MachineRoleMissing",
    "MachineRoles",
    "format_capabilities",
    "gateway_api_base",
    "gateway_auth_headers",
    "gateway_bearer",
    "roles_from_flags",
    "validate_machine_name",
]


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
