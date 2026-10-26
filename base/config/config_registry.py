"""Flat config-field registry — the EnvRegistry's Settings-field half.

The single place the registry is BUILT: walking the per-domain sub-model
classes (class metadata only — no Settings instantiation, so `dotenv_boot`,
which runs before Settings exists, can consume the registry's projections
through `base/host/env/registry.py` without a timing dependency).

It sits in `base.config`, but importing it never constructs Settings: the package
boots lite (`base/config/_lite.py`), which defers the Settings build, and the
module-level `FIELD_REFS` / `FIELD_INFOS` faces of `base.config` are served from this
registry without upgrading. The sub-model class imports are deferred into the build
(first use); the build completes against a partially-initialized `base.config` when
one is already in flight (for example, the standalone `load_meiva_env()` boot path),
and the double build that results is idempotent. Code that must not import
`base.config` at all (the `meiva config` repair path) takes the wire vocabulary it
needs from `base/api_contracts/config.py`, not from here.

Settings fields and their `json_schema_extra` metadata are declared on the
owning domain sub-model. This registry exposes those declarations; non-Settings
keys are declared in `base/host/env/registry.py`. Consumer projections combine
scope/capability metadata with their explicit classification rules. Declaring a
field does not decide every projection automatically. See
`base/host/env/docs/registry.meiva.okf.md` for the current projection boundaries.

Everything here is a pure function of the class declarations: building reads no
environment and constructs no Settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from base.api_contracts.config import Capability as Capability
from base.config.profiles import (
    ALLOWED_CAPABILITIES as ALLOWED_CAPABILITIES,
)
from base.config.profiles import (
    ALLOWED_RESTART_REQUIRED as ALLOWED_RESTART_REQUIRED,
)
from base.config.profiles import (
    RESTART_REQUIRED_PROFILE as RESTART_REQUIRED_PROFILE,
)
from base.config.profiles import (
    DomainModelLike as _DomainModelLike,
)
from base.config.profiles import (
    FieldInfoLike as _FieldInfoLike,
)
from base.config.profiles import (
    FieldRef as _FieldRef,
)
from base.config.profiles import (
    Lifecycle as Lifecycle,
)
from base.config.profiles import (
    SchemaExtra as SchemaExtra,
)
from base.config.profiles import (
    field_editor_type as field_editor_type,
)
from base.config.profiles import (
    schema_extra as schema_extra,
)
from base.config.profiles import (
    validate_field_metadata,
)

DOMAIN_MODELS: tuple[tuple[str, str, str | type[object] | _DomainModelLike, str], ...] = (
    ("lm", "LLM", "LmSettings", "agent-runner"),
    ("sandbox", "Sandbox", "SandboxSettings", "agent-runner"),
    ("agent", "Agent", "AgentSettings", "agent-runner"),
    ("web", "Web", "WebSettings", "agent-runner"),
    ("to_user", "To user", "ToUserSettings", "common"),
    ("gateway", "Gateway", "GatewaySettings", "gateway"),
    ("daemon", "Daemon", "DaemonSettings", "gateway"),
    ("alerts", "Alerts", "AlertsSettings", "gateway"),
    ("data_plane", "Data plane", "DataPlaneSettings", "gateway"),
    ("services", "Services", "ServiceSettings", "gateway"),
    ("observability", "Observability", "ObservabilitySettings", "common"),
    ("display", "Display", "DisplaySettings", "common"),
    ("packages", "Packages", "PackagesSettings", "agent-runner"),
    ("feishu", "Feishu", "FeishuSettings", "gateway"),
    ("telegram", "Telegram", "TelegramSettings", "gateway"),
    ("walg", "WAL-G backup", "WalgSettings", "gateway"),
    ("general", "General", "GeneralSettings", "common"),
)

DOMAIN_ATTRS = frozenset(attr for attr, _label, _model, _cap in DOMAIN_MODELS)


# meiva:note
# The sub-model class behind each `DOMAIN_MODELS` name.
def _model_classes() -> dict[str, Any]:
    from base.config.domains.agent.settings import AgentSettings
    from base.config.domains.channels.feishu import FeishuSettings
    from base.config.domains.channels.telegram import TelegramSettings
    from base.config.domains.daemon.settings import DaemonSettings
    from base.config.domains.display import DisplaySettings
    from base.config.domains.gateway import GatewaySettings
    from base.config.domains.general import GeneralSettings
    from base.config.domains.lm import LmSettings
    from base.config.domains.observability.alerts import AlertsSettings
    from base.config.domains.observability.settings import ObservabilitySettings
    from base.config.domains.packages import PackagesSettings
    from base.config.domains.sandbox import SandboxSettings
    from base.config.domains.services.settings import ServiceSettings
    from base.config.domains.storage.data_plane import DataPlaneSettings
    from base.config.domains.storage.walg import WalgSettings
    from base.config.domains.to_user import ToUserSettings
    from base.config.domains.web import WebSettings

    return {
        "LmSettings": LmSettings,
        "SandboxSettings": SandboxSettings,
        "AgentSettings": AgentSettings,
        "WebSettings": WebSettings,
        "ToUserSettings": ToUserSettings,
        "GatewaySettings": GatewaySettings,
        "DaemonSettings": DaemonSettings,
        "AlertsSettings": AlertsSettings,
        "DataPlaneSettings": DataPlaneSettings,
        "ServiceSettings": ServiceSettings,
        "ObservabilitySettings": ObservabilitySettings,
        "DisplaySettings": DisplaySettings,
        "PackagesSettings": PackagesSettings,
        "FeishuSettings": FeishuSettings,
        "TelegramSettings": TelegramSettings,
        "WalgSettings": WalgSettings,
        "GeneralSettings": GeneralSettings,
    }


def validate_domain_models(
    domain_models: tuple[tuple[str, str, str | type[object] | _DomainModelLike, str], ...],
) -> dict[str, _FieldRef]:
    reg: dict[str, _FieldRef] = {}
    for attr, label, model_name, default_capability in domain_models:
        model: Any = _model_classes()[model_name] if isinstance(model_name, str) else model_name
        fields_map: Mapping[str, _FieldInfoLike] = model.model_fields
        for name, info in fields_map.items():
            if name in reg:
                raise RuntimeError(
                    f"config field name {name!r} is not unique across sub-models "
                    f"({reg[name].domain} vs {attr}) — flat keying (wire/.env/bootstrap) "
                    f"requires globally-unique field names"
                )
            extra = schema_extra(info)
            capability = validate_field_metadata(name, attr, extra, default_capability)
            reg[name] = _FieldRef(
                name=name,
                domain=attr,
                group=label,
                capability=capability,
                info=info,
            )
    return reg


@lru_cache(maxsize=1)
def build_registry() -> dict[str, _FieldRef]:
    """Walk the sub-models into a flat name->owner registry, failing fast on an
    ownership-metadata mistake. The structural checks that used to live in a
    standalone `lint_config_scope` hook run here — a field with a bad or missing
    `scope` (which drives BOOTSTRAP_FIELDS + write routing) can never load, so
    the drift the lint guarded against is impossible rather than merely flagged.
    """
    return validate_domain_models(DOMAIN_MODELS)


fields = build_registry


# Leaf FieldInfo by name — the compat replacement for the old flat
# `Settings.model_fields` for code that iterated field metadata. Built lazily
# (PEP 562) — constructing it at module level would run `build_registry()`
# during this module's import, whose deferred `base.config` package import
# re-enters this module mid-initialization (the package __init__ re-imports
# `field_alias` etc.) and blows up on a clean env (`ImportError: cannot import
# name 'field_alias' from partially initialized module ...` — Task #1099).
@lru_cache(maxsize=1)
def field_infos() -> dict[str, _FieldInfoLike]:
    return {n: r.info for n, r in fields().items()}


def __getattr__(name: str) -> dict[str, _FieldInfoLike]:
    if name == "FIELD_INFOS":
        return field_infos()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def field_alias(name: str) -> str:
    """The `.env` / env-var alias a field reads from (serialization alias wins)."""
    info = fields()[name].info
    return info.serialization_alias or info.alias or name.upper()


def field_domain(name: str) -> str:
    """The `settings` attribute holding this field (e.g. 'lm')."""
    return fields()[name].domain


def per_agent_field_names() -> set[str]:
    """Leaf field names flagged `json_schema_extra={"per_agent": True}` — the
    framework fields a spawn/restart config overlay may override."""
    return {
        name for name, ref in fields().items() if schema_extra(ref.info).get("per_agent") is True
    }
