from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast, overload

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError

__all__ = [
    "all_plugin_configs",
    "config_from_image",
    "get_plugin_config",
    "is_per_agent_field",
    "process_plugin_config",
    "service_config_packet",
]


class _AgentConfigSlices(Protocol):
    def plugin_config(self, plugin: str) -> BaseModel: ...
    def plugin_configs(self) -> dict[str, BaseModel]: ...


def config_from_image[C: BaseModel](cls: type[C], content: str, config_path: Path) -> C:
    try:
        disk_data = json.loads(content)
    except json.JSONDecodeError as e:
        raise InvalidConfigData(
            f"plugin config disk image JSON parse failed ({config_path}): {e}"
        ) from e

    if not isinstance(disk_data, dict):
        raise InvalidConfigData(
            "plugin config disk image top-level must be a JSON object, got "
            f"{type(disk_data).__name__} ({config_path})"
        )
    disk_data = cast("dict[str, Any]", disk_data)

    disk_keys = set(disk_data.keys())
    cls_keys = set(cls.model_fields.keys())
    if disk_keys != cls_keys:
        added = cls_keys - disk_keys
        removed = disk_keys - cls_keys
        raise SchemaDriftError(
            f"plugin config disk image schema drift ({config_path}): "
            f"added={sorted(added)} removed={sorted(removed)}. "
            f"Run `meiva plugins update` to auto-merge."
        )

    try:
        return cls(**disk_data)
    except ValidationError as e:
        raise InvalidConfigData(
            f"plugin config disk image Pydantic validation failed ({config_path}): {e}"
        ) from e


@overload
def get_plugin_config(plugin: str, slices: _AgentConfigSlices) -> BaseModel: ...
@overload
def get_plugin_config[T: BaseModel](plugin: str, slices: _AgentConfigSlices, cls: type[T]) -> T: ...


def get_plugin_config(
    plugin: str, slices: _AgentConfigSlices, cls: type[BaseModel] | None = None
) -> BaseModel:
    _ = cls
    return slices.plugin_config(plugin)


def all_plugin_configs(slices: _AgentConfigSlices) -> dict[str, BaseModel]:
    return slices.plugin_configs()


def process_plugin_config(plugin: str, configs: Mapping[str, BaseModel]) -> BaseModel:
    return configs[plugin]


def _schema_extra(info: FieldInfo) -> dict[str, Any]:
    extra = info.json_schema_extra
    return cast("dict[str, Any]", extra) if isinstance(extra, dict) else {}


def _field_is_sensitive(extra: object) -> bool:
    if not isinstance(extra, dict):
        return False
    extras = cast(dict[str, Any], extra)
    return extras.get("sensitive") is True


def is_per_agent_field(plugin: str, field: str, configs: Mapping[str, BaseModel]) -> bool:
    instance = configs.get(plugin)
    if instance is None:
        return False
    info = type(instance).model_fields.get(field)
    if info is None:
        return False
    return bool(_schema_extra(info).get("per_agent", False))


def service_config_packet(plugin: str, config: BaseModel) -> str:
    for name, field in type(config).model_fields.items():
        if _field_is_sensitive(field.json_schema_extra):
            raise InvalidConfigData(f"service config {plugin}.{name} is sensitive")
    return json.dumps(
        {"plugin": plugin, "config": config.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
