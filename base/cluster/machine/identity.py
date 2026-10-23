import enum
from collections.abc import Callable, Iterable
from typing import Literal

__all__ = [
    "MachineIdentity",
    "MachineNameMissing",
    "MachineRole",
    "MachineRoleInvalid",
    "MachineRoleMissing",
    "MachineRoles",
    "format_capabilities",
    "roles_from_flags",
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


class _Unset(enum.Enum):
    TOKEN = enum.auto()


_UNSET = _Unset.TOKEN


# meiva:note
# Explicitly owned machine identity cache. Each field resolves lazily and
# independently, sharing one reset() for test isolation and one set() for explicit injection.
class MachineIdentity:
    def __init__(
        self,
        *,
        name: Callable[[], str],
        role: Callable[[], MachineRoles],
        host: Callable[[], str],
        description: Callable[[], str | None],
    ) -> None:
        self._read_name = name
        self._read_role = role
        self._read_host = host
        self._read_description = description
        self._name: str | None = None
        self._role: MachineRoles | None = None
        self._description: str | None = None
        self._description_resolved: bool = False
        self._host: str | None = None

    def name(self) -> str:
        if self._name is None:
            self._name = validate_machine_name(self._read_name())
        return self._name

    def role(self) -> MachineRoles:
        if self._role is None:
            self._role = _coerce_roles(self._read_role())
        return self._role

    def host(self) -> str:
        if self._host is None:
            self._host = self._read_host().strip() or "localhost"
        return self._host

    def description(self) -> str | None:
        if not self._description_resolved:
            description = self._read_description()
            self._description = description.strip() or None if description else None
            self._description_resolved = True
        return self._description

    def set(
        self,
        *,
        name: str | _Unset = _UNSET,
        role: str | Iterable[str] | _Unset = _UNSET,
        description: str | _Unset | None = _UNSET,
        host: str | _Unset = _UNSET,
    ) -> None:
        if name is not _UNSET:
            self._name = name
        if role is not _UNSET:
            self._role = _coerce_roles(role)
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


def _coerce_roles(role: str | Iterable[str]) -> MachineRoles:
    if isinstance(role, str):
        return _parse_roles(role)
    return _parse_roles(",".join(role))
