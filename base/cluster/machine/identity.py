from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "MachineFacts",
    "MachineIdentityReader",
    "MachineNameMissing",
    "MachineRole",
    "MachineRoleInvalid",
    "MachineRoleMissing",
    "MachineRoles",
    "Unset",
    "format_capabilities",
    "roles_from_flags",
    "roles_from_input",
    "validate_machine_name",
]

MachineRole = Literal["gateway", "agent-runner", "observability-station"]
MachineRoles = frozenset[str]
_VALID_CAPABILITIES: frozenset[str] = frozenset(
    ("gateway", "agent-runner", "observability-station")
)


# meiva:note
# `MEIVA_MACHINE_NAME` is not set — multi-machine setup is incomplete.
class MachineNameMissing(RuntimeError):  # noqa: N818
    pass


# meiva:note
# This host serves no capability (every MEIVA_MACHINE_SERVE_* flag off or unset).
class MachineRoleMissing(RuntimeError):  # noqa: N818
    pass


# meiva:note
# `role` contains a token that is not `gateway`, `agent-runner`, or `observability-station`, or is empty.
class MachineRoleInvalid(ValueError):  # noqa: N818
    pass


class Unset(enum.Enum):
    TOKEN = enum.auto()


_UNSET = Unset.TOKEN


# meiva:note
# The read-only machine identity contract shared by an owned configuration reader and frozen
# declared facts. Mutation belongs only to the configuration owner, never to this boundary.
class MachineIdentityReader(Protocol):
    def name(self) -> str: ...

    def role(self) -> MachineRoles: ...

    def host(self) -> str: ...

    def description(self) -> str | None: ...


# meiva:note
# A machine identity built from facts, without readers, a cache or mutation operations. Missing
# name/capabilities are explicit facts: only the corresponding read raises the existing Missing
# error. Unknown capability tokens are invalid at construction, including the raw-value factory.
@dataclass(frozen=True)
class MachineFacts:
    _name: str
    _roles: MachineRoles
    _host: str
    _description: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "_name", self._name.strip())
        object.__setattr__(self, "_roles", _declared_roles(self._roles))
        object.__setattr__(self, "_host", self._host.strip() or "localhost")
        description = self._description.strip() if self._description else None
        object.__setattr__(self, "_description", description or None)

    @classmethod
    def of(
        cls,
        *,
        name: str,
        role: str | Iterable[str],
        host: str = "localhost",
        description: str | None = None,
    ) -> MachineFacts:
        return cls(name, _declared_roles(role), host, description)

    @classmethod
    def capture(
        cls,
        identity: MachineIdentityReader,
        *,
        name: str | Unset = _UNSET,
        role: str | Iterable[str] | Unset = _UNSET,
        host: str | Unset = _UNSET,
        description: str | Unset | None = _UNSET,
    ) -> MachineFacts:
        if isinstance(name, Unset):
            try:
                name = identity.name()
            except MachineNameMissing:
                name = ""
        if isinstance(role, Unset):
            try:
                role = identity.role()
            except MachineRoleMissing:
                role = frozenset()
        return cls.of(
            name=name,
            role=role,
            host=identity.host() if isinstance(host, Unset) else host,
            description=identity.description() if isinstance(description, Unset) else description,
        )

    def replaced(
        self,
        *,
        name: str | Unset = _UNSET,
        role: str | Iterable[str] | Unset = _UNSET,
        host: str | Unset = _UNSET,
        description: str | Unset | None = _UNSET,
    ) -> MachineFacts:
        return type(self).of(
            name=self._name if isinstance(name, Unset) else name,
            role=self._roles if isinstance(role, Unset) else role,
            host=self._host if isinstance(host, Unset) else host,
            description=self._description if isinstance(description, Unset) else description,
        )

    def name(self) -> str:
        return validate_machine_name(self._name)

    def role(self) -> MachineRoles:
        if not self._roles:
            return roles_from_flags(
                serve_gateway=False,
                serve_agent_runner=False,
                serve_observability_station=False,
            )
        return self._roles

    def host(self) -> str:
        return self._host

    def description(self) -> str | None:
        return self._description


# meiva:note
# Render capability flags as one human label ("gateway + agent-runner", "none", etc.).
def format_capabilities(
    serve_gateway: bool,  # noqa: FBT001
    serve_agent_runner: bool,  # noqa: FBT001
    serve_observability_station: bool = False,  # noqa: FBT001, FBT002
) -> str:
    caps = [
        name
        for name, on in (
            ("gateway", serve_gateway),
            ("agent-runner", serve_agent_runner),
            ("observability-station", serve_observability_station),
        )
        if on
    ]
    return " + ".join(caps) or "none"


# meiva:note
# Normalize a configured name and reject an uninitialized machine.
def validate_machine_name(value: str) -> str:
    env = value.strip()
    if env:
        return env
    raise MachineNameMissing(
        "machine name not set — `meiva init --machine-name <name>` records it for a new "
        "home; for an initialized one set MEIVA_MACHINE_NAME in its `.env` (e.g. host-a / host-b)."
    )


# meiva:note
# Resolve the declared capability set without reading process settings.
def roles_from_flags(
    *,
    serve_gateway: bool,
    serve_agent_runner: bool,
    serve_observability_station: bool,
) -> MachineRoles:
    caps = {
        cap
        for cap, on in (
            ("gateway", serve_gateway),
            ("agent-runner", serve_agent_runner),
            ("observability-station", serve_observability_station),
        )
        if on
    }
    if not caps:
        raise MachineRoleMissing(
            "this host serves neither gateway, agent-runner, nor observability-station — set "
            "MEIVA_MACHINE_SERVE_GATEWAY and/or MEIVA_MACHINE_SERVE_AGENT_RUNNER and/or "
            "MEIVA_MACHINE_SERVE_OBSERVABILITY_STATION (or run `meiva init --serve-gateway` / "
            "`--serve-agent-runner` / `--serve-observability-station` on a new home; a single "
            "box serves both gateway and agent-runner)."
        )
    return frozenset(caps)


def _parse_roles(value: str) -> MachineRoles:
    tokens = frozenset(t.strip() for t in value.split(",") if t.strip())
    unknown = tokens - _VALID_CAPABILITIES
    if unknown or not tokens:
        raise MachineRoleInvalid(
            f"machine_role={value!r} invalid; comma-separated tokens, each "
            f"'gateway', 'agent-runner', or 'observability-station' (got unknown {sorted(unknown)!r})."
            if unknown
            else f"machine_role={value!r} invalid; needs at least one of "
            "'gateway' / 'agent-runner' / 'observability-station'."
        )
    return tokens


def roles_from_input(role: str | Iterable[str]) -> MachineRoles:
    if isinstance(role, str):
        return _parse_roles(role)
    return _parse_roles(",".join(role))


# meiva:note
# Freeze supplied capability tokens once. Empty tokens mean an explicitly unconfigured identity;
# unknown nonempty tokens still use the canonical role parser's fail-fast boundary.
def _declared_roles(role: str | Iterable[str]) -> MachineRoles:
    supplied = role.split(",") if isinstance(role, str) else role
    tokens = frozenset(token.strip() for token in supplied if token.strip())
    return _parse_roles(",".join(sorted(tokens))) if tokens else frozenset()
