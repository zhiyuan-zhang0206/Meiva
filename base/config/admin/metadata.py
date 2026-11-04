"""Frontend config-panel metadata export (a consumer of the field registry).

`ConfigFieldMeta` / `get_config_metadata` / `env_override_values` serialize the
flat field registry + current values for GET /api/config; the write path's
"keep existing value" sentinel lives here too. Split out of
`base/config/__init__.py` for the design's line budget (R2 convergence point
A re-split): the registry is built in `base/config/config_registry.py`, Settings and
its boot consumers stay in `__init__.py`.

Imports of the config package are function-level (this module is imported by
`base/config/__init__.py` at its tail for the re-exports; a top-level import
would be circular).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr
from pydantic_core import PydanticUndefined

from base.config.config_registry import (
    Capability,
    field_alias,
    field_editor_type,
    fields,
    schema_extra,
    scope_of,
)
from base.host.boundary.json_values import is_object_list

# Stand-in a sensitive field carries in `raw_overrides` instead of its cleartext
# value: the panel/CLI round-trips it unchanged on a full-replace PUT, and the
# write path treats it as "keep the existing .env value" (neither rewrite nor
# unset). This keeps secrets off the wire without a full-replace clobbering them.
CONFIG_UNCHANGED_SENTINEL = "__meiva_unchanged__"


type _ConfigFieldValue = str | int | float | bool | list[str] | list[float] | SecretStr | None
type _ConfigChoices = list[str] | None


# meiva:note
# A settings value as the config panel carries it: a path becomes its text and a list keeps only
# the element types the panel renders. Every other value is a field type this registry does not
# declare, which fails here instead of reaching the wire in a shape no reader expects.
def _panel_value(value: object) -> _ConfigFieldValue:
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, str | int | float | SecretStr):
        return value
    if is_object_list(value):
        texts = [item for item in value if isinstance(item, str)]
        if len(texts) == len(value):
            return texts
        numbers = [float(item) for item in value if isinstance(item, int | float)]
        if len(numbers) == len(value):
            return numbers
    raise TypeError(f"unsupported config field value of type {type(value).__name__}")


class ConfigMetadataAuthority(Protocol):
    def current_field_values(self) -> Mapping[str, object]: ...

    def read_env_aliases(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class ConfigFieldMeta:
    """Full metadata for a single config field — serialized by GET /api/config for the frontend."""

    name: str
    label: str
    field_type: str
    current_value: object
    default_value: _ConfigFieldValue
    description: str
    group: str
    restart_required: str
    _: KW_ONLY
    writable: bool
    sensitive: bool
    env_var: str
    scope: str
    # meiva:note
    # gateway | agent-runner | common — conceptual ownership, not panel display grouping.
    capability: Capability
    remote_writable: bool
    per_agent: bool
    # meiva:note
    # For an "enum" field, the allowed values (the Literal members) the
    # frontend renders as a fixed-option select; None for every other type.
    choices: _ConfigChoices = None
    owner: str | None = None


def get_config_metadata(*, authority: ConfigMetadataAuthority) -> list[ConfigFieldMeta]:
    """Walk every core sub-model's fields and extract the metadata the frontend needs.

    Plugin-owned fields are not core: `base.packages.plugin_config_admin`
    composes them on top (`panel_metadata`)."""
    values = authority.current_field_values()
    result: list[ConfigFieldMeta] = []
    for name, ref in fields().items():
        field_info = ref.info
        extra = schema_extra(field_info)
        if not field_info.title or not field_info.title.strip():
            raise ValueError(f"config field {name!r} must declare a display title")
        type_name, choices = field_editor_type(field_info.annotation)

        current = _panel_value(values[name])

        if field_info.default is PydanticUndefined:
            # Required fields (db_url / redis_url etc. with no default) leaking the
            # sentinel would cause FastAPI's ConfigFieldView serialization of the
            # whole response to blow up with 500.
            default = None
        else:
            default = _panel_value(field_info.default)

        # remote_writable is required on host-scope fields (hard index raises
        # KeyError if a host field forgot to declare it); non-host fields default False.
        scope = scope_of(extra)
        remote_writable = False
        if scope == "host":
            declared = extra.get("remote_writable")
            if declared is None:
                raise KeyError("remote_writable")
            remote_writable = declared

        result.append(
            ConfigFieldMeta(
                name=name,
                label=field_info.title,
                field_type=type_name,
                current_value=current,
                default_value=default,
                description=field_info.description or "",
                group=ref.group,
                restart_required=extra.get("restart_required", "all"),
                writable=extra.get("writable", True),
                sensitive=extra.get("sensitive", False),
                env_var=field_alias(name),
                scope=scope,
                capability=ref.capability,
                remote_writable=remote_writable,
                per_agent=extra.get("per_agent") is True,
                choices=choices,
            )
        )
    return result


# meiva:note
# field name -> current value for every field explicitly set in this unit's
# `.env` that a PUT /api/config would accept.
#
# This is the editable override set the config panel deltas against. A field is
# included only if it is `writable`; a host-scope field additionally requires
# `remote_writable` UNLESS `local` (a self-target edit), since `writable` already
# means "a human may edit it on its own host" and `remote_writable` only gates
# editing a *remote* host's field. That keeps read-only fields (`db_url`) out and,
# for a remote target, the connection / identity values the first `meiva start` wrote — so
# the whole set round-trips back through a PUT without rejection.
#
# Core only unless the caller supplies the plugin-owned `plugin_metadata` and the
# `plugin_values` present in their images (`config_admin.panel_overrides` does).
def env_override_values(
    *,
    authority: ConfigMetadataAuthority,
    local: bool = False,
    plugin_metadata: Sequence[ConfigFieldMeta] = (),
    plugin_values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    extra_values = dict(plugin_values or {})
    aliases = authority.read_env_aliases()
    set_fields = {name for name in fields() if field_alias(name) in aliases} | extra_values.keys()
    values = {**authority.current_field_values(), **extra_values}
    out: dict[str, object] = {}
    for meta in [*get_config_metadata(authority=authority), *plugin_metadata]:
        if meta.name not in set_fields or not meta.writable:
            continue
        if meta.scope == "host" and not local and not meta.remote_writable:
            continue
        if meta.sensitive:
            # Never put a secret's cleartext on the wire. The panel/CLI carries the
            # sentinel and PUTs it back unchanged; the write path preserves the
            # existing .env value (set the field with a real value to change it,
            # `meiva config unset` to clear it).
            out[meta.name] = CONFIG_UNCHANGED_SENTINEL
            continue
        value = values[meta.name]
        if isinstance(value, Path):
            value = str(value)
        out[meta.name] = value
    return out
