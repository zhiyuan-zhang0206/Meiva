# meiva:note
# Configuration-backed machine identity owner for composition roots.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable

from base.cluster.machine.gateway_access import GatewayAccess
from base.cluster.machine.identity import (
    MachineRoles,
    Unset,
    roles_from_flags,
    roles_from_input,
    validate_machine_name,
)
from base.config import ConfigBoot
from base.host.net import require_cluster_secret

__all__ = ["ConfiguredMachineIdentity", "daemon_acceptance"]

_UNSET = Unset.TOKEN


# meiva:note
# SHA-256 digests of the bearers a gateway-side daemon's authenticated endpoint accepts.
# The acceptor's half of `gateway_bearer(access)`: the daemons that take calls from the
# gateway (the ops server, im_bridge's `/send`) accept machine API tokens, never the
# human cluster secret.
#
# The gateway dials with its gateway-class token and a runner-class process of the unit with its
# runner token, so a daemon accepts exactly its write generation's two tokens. A remote unit
# takes the gateway token digest its installed capability carries plus its own runner token's:
# it never holds the gateway token or the human secret. The gateway home takes the active
# generation's two tokens (none while no generation is active: fail closed).
#
# The one place the human secret is still accepted is a remote-managed data plane
# (`access.remote_plane`): it keeps no write generations and issues no token, so
# its gateway-local services present, and its daemons accept, the human secret. That branch
# is the exception to "outside the gateway, the CLI and the root, no component holds the
# cluster secret" (docs/decisions/data/security/2026-10-03-cluster-secret-contraction.md); the live
# deployment's data plane is local, and this branch only runs when `is_remote`.
def daemon_acceptance(access: GatewayAccess) -> frozenset[str]:
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


# meiva:note
# Explicitly owned machine identity resource backed by a root's ConfigBoot.
# Construction reads no configuration fields; each field resolves lazily on first read
# and caches independently. reset() clears only this owner's cached/overridden fields.
class ConfiguredMachineIdentity:
    def __init__(self, config: ConfigBoot) -> None:
        self._config = config
        self._name: str | None = None
        self._role: MachineRoles | None = None
        self._description: str | None = None
        self._description_resolved: bool = False
        self._host: str | None = None

    def name(self) -> str:
        if self._name is None:
            self._name = validate_machine_name(self._config.view.general.machine_name)
        return self._name

    def role(self) -> MachineRoles:
        if self._role is None:
            self._role = roles_from_flags(
                serve_gateway=bool(self._config.view.general.machine_serve_gateway),
                serve_agent_runner=bool(self._config.view.general.machine_serve_agent_runner),
                serve_observability_station=bool(
                    self._config.view.general.machine_serve_observability_station
                ),
            )
        return self._role

    def host(self) -> str:
        if self._host is None:
            host: str = self._config.view.general.machine_host
            self._host = host.strip() or "localhost"
        return self._host

    def description(self) -> str | None:
        if not self._description_resolved:
            description: str = self._config.view.general.machine_description
            self._description = description.strip() or None if description else None
            self._description_resolved = True
        return self._description

    def set(
        self,
        *,
        name: str | Unset = _UNSET,
        role: str | Iterable[str] | Unset = _UNSET,
        description: str | Unset | None = _UNSET,
        host: str | Unset = _UNSET,
    ) -> None:
        if name is not _UNSET:
            self._name = name
        if role is not _UNSET:
            self._role = roles_from_input(role)
        if description is not _UNSET:
            self._description = description
            self._description_resolved = True
        if host is not _UNSET:
            self._host = host

    def reset(self) -> None:
        self._name = None
        self._role = None
        self._description = None
        self._description_resolved = False
        self._host = None
