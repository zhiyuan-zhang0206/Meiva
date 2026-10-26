# meiva:note
# Profile domains follow actual reads in each process kind's import closure, independently of config-panel capability labels. Launchers select gateway, agent, or runner; unknown names fail fast. Excluded domains raise an actionable AttributeError in eager and lite access, and dynamic consumers can use has_domain. Runner support daemons do not consume the agent domain now that shared documents always use Markdown; agent execution belongs to the agent profile.
# meiva:link base/config/docs/process-profiles.meiva.okf.md
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import UnionType
from typing import (
    Any,
    Literal,
    NotRequired,
    Protocol,
    TypedDict,
    Union,
    get_args,
    get_origin,
)

from base.api_contracts.config import Capability
from base.host.env.process_profile import ProcessProfile

PROCESS_PROFILES: dict[ProcessProfile, frozenset[str]] = {
    "gateway": frozenset(
        {
            "data_plane",
            "gateway",
            "general",
            "services",
            "daemon",
            "alerts",
            "lm",
            "telegram",
            "feishu",
            "observability",
            "display",
            "to_user",
        }
    ),
    "agent": frozenset(
        {
            "agent",
            "lm",
            "sandbox",
            "web",
            "data_plane",
            "general",
            "observability",
            "display",
            "gateway",
            "services",
            "daemon",
            "to_user",
        }
    ),
    "runner": frozenset(
        {
            "services",
            "daemon",
            "general",
            "data_plane",
            "gateway",
            "lm",
            "sandbox",
            "observability",
            "to_user",
        }
    ),
}


PROFILE_UNSET = "!unset!"


def profile_domain_error(profile: str, domain: str) -> AttributeError:
    return AttributeError(
        f"'{profile}' process profile does not construct the {domain!r} config "
        f"domain (per-process config, Task #856) — nothing in this process "
        f"kind reads settings.{domain}. If this read is legitimate, add the "
        f"domain to the '{profile}' profile in PROCESS_PROFILES AND to the "
        f"consumption-matrix guard (cli/commands/lifecycle/tests/startup/test_gateway_consumer_guard.py); "
        f"otherwise move the read to the process kind that owns the domain. "
        f"Dynamic code can check settings.has_domain({domain!r}) first."
    )


# meiva:note
# The pydantic `FieldInfo` surface this registry reads.
class FieldInfoLike(Protocol):
    @property
    def alias(self) -> str | None: ...

    @property
    def annotation(self) -> object: ...

    @property
    def default(self) -> object: ...

    @property
    def default_factory(self) -> Callable[..., object] | None: ...

    @property
    def description(self) -> str | None: ...

    @property
    def serialization_alias(self) -> str | None: ...

    @property
    def validation_alias(self) -> object: ...

    @property
    def json_schema_extra(self) -> dict[str, Any] | Callable[[dict[str, Any]], None] | None: ...

    def get_default(
        self, *, call_default_factory: Literal[True], validated_data: dict[str, Any] | None = None
    ) -> object: ...


class DomainModelLike(Protocol):
    @property
    def model_fields(self) -> Mapping[str, FieldInfoLike]: ...


class SchemaExtra(TypedDict):
    scope: str
    remote_writable: bool
    writable: bool
    bootstrap: bool
    restart_required: str
    sensitive: bool
    capability: NotRequired[Capability]
    per_agent: NotRequired[bool]
    lifecycle: NotRequired[str]
    env_var: NotRequired[str]


@dataclass(frozen=True)
class FieldRef:
    name: str
    domain: str
    group: str
    capability: Capability
    info: FieldInfoLike


ALLOWED_CAPABILITIES = frozenset({"gateway", "agent-runner", "common"})
_ALLOWED_SCOPES = frozenset({"cluster-pinned", "cluster-default", "host", "agent"})
_ALLOWED_LIFECYCLES = frozenset({"frozen", "live"})
Lifecycle = Literal["frozen", "live"]

ALLOWED_RESTART_REQUIRED = frozenset({"", "agent", "all", "gateway", "ops", "schedule"})
RESTART_REQUIRED_PROFILE: dict[str, ProcessProfile] = {
    "agent": "agent",
    "gateway": "gateway",
    "ops": "runner",
}


# meiva:note
# The field's `json_schema_extra` as a plain dict (empty if unset / callable).
def schema_extra(info: FieldInfoLike) -> SchemaExtra:
    extra = info.json_schema_extra
    out: Any = {} if callable(extra) or extra is None else extra
    return out


# meiva:note
# Fail fast on one field's ownership metadata; returns its validated capability.
def validate_field_metadata(
    name: str, attr: str, extra: SchemaExtra, default_capability: str
) -> Capability:
    scope = extra.get("scope")
    capability = extra.get("capability", default_capability)
    if capability not in ALLOWED_CAPABILITIES:
        raise RuntimeError(
            f"config field {name!r} ({attr}) has capability={capability!r}; must be "
            f"one of {sorted(ALLOWED_CAPABILITIES)} — it names the config-panel "
            f"section the field renders under"
        )
    if scope not in _ALLOWED_SCOPES:
        raise RuntimeError(
            f"config field {name!r} ({attr}) has scope={scope!r}; must be one of "
            f"{sorted(_ALLOWED_SCOPES)} — scope drives BOOTSTRAP_FIELDS distribution + "
            f".env write routing"
        )
    _validate_scope_rules(name, extra, scope)
    _validate_lifecycle(name, attr, extra)
    _validate_restart_required(name, attr, extra)
    if capability == "gateway":
        return "gateway"
    if capability == "agent-runner":
        return "agent-runner"
    return "common"


def _validate_scope_rules(name: str, extra: SchemaExtra, scope: object) -> None:
    if scope == "host" and not isinstance(extra.get("remote_writable"), bool):
        raise RuntimeError(
            f"host-scope field {name!r} must declare remote_writable: bool "
            f"(got {extra.get('remote_writable')!r})"
        )
    if scope != "host" and extra.get("remote_writable") is True:
        raise RuntimeError(
            f"non-host field {name!r} sets remote_writable=True — only host-scope "
            f"fields are remote-writable"
        )
    if scope == "cluster-default" and extra.get("per_agent") is not True:
        raise RuntimeError(
            f"cluster-default field {name!r} must be per_agent=True (the per-agent override gate)"
        )


def _validate_lifecycle(name: str, attr: str, extra: SchemaExtra) -> None:
    lifecycle = extra.get("lifecycle")
    if extra.get("per_agent") is True:
        if lifecycle not in _ALLOWED_LIFECYCLES:
            raise RuntimeError(
                f"per-agent field {name!r} ({attr}) has lifecycle={lifecycle!r}; must be "
                f"one of {sorted(_ALLOWED_LIFECYCLES)} — 'frozen' is stamped into "
                f"agents_meta.birth_config at spawn and replayed for the agent's life, "
                f"'live' is re-read from cluster config at every process start. There is "
                f"no default: the choice is a semantic ruling about whether the field is "
                f"the agent's identity material or an operational knob."
            )
    elif lifecycle is not None:
        raise RuntimeError(
            f"field {name!r} ({attr}) declares lifecycle={lifecycle!r} but is not "
            f"per_agent=True — the lifecycle axis only applies to fields that HAVE a "
            f"per-agent instance to freeze; cluster-scope config is read live by "
            f"whatever process next starts"
        )


def _validate_restart_required(name: str, attr: str, extra: SchemaExtra) -> None:
    restart_required = extra.get("restart_required", "")
    if restart_required not in ALLOWED_RESTART_REQUIRED:
        raise RuntimeError(
            f"config field {name!r} ({attr}) has restart_required={restart_required!r}; "
            f"must be one of {sorted(ALLOWED_RESTART_REQUIRED)} — it names the process "
            f"the operator must restart after a change, and the panel/CLI prompt from it"
        )
    profile_kind = RESTART_REQUIRED_PROFILE.get(restart_required)
    if profile_kind is not None:
        profile = PROCESS_PROFILES[profile_kind]
        if attr not in profile:
            raise RuntimeError(
                f"field {name!r} ({attr}) declares restart_required={restart_required!r} "
                f"but its domain is not in the {profile_kind!r} process profile "
                f"({sorted(profile)}) — the named process kind "
                f"does not consume this field, so the operator would restart the wrong "
                f"process and the change would silently not take effect. Point it at "
                f"the kind that actually reads it (the im_bridge-consumed telegram/feishu/"
                f"im_* fields are 'gateway'), or use 'all' or 'schedule'."
            )


# meiva:note
# Return the config editor type and enum choices declared by `annotation`.
def field_editor_type(annotation: object) -> tuple[str, list[str] | None]:
    union_args = get_args(annotation)
    if get_origin(annotation) in (Union, UnionType) and type(None) in union_args:
        non_none = [item for item in union_args if item is not type(None)]
        if len(non_none) == 1:
            annotation = non_none[0]
    if annotation is bool:
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    if get_origin(annotation) is Literal:
        return "enum", [str(item) for item in get_args(annotation)]
    return "string", None
