from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, ValidationError

from base.packages.plugins import enable_config
from base.packages.plugins.config_face import (
    declared_config_class,
    disk_image_path,
    enabled_config_classes,
    read_config_image,
)
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError


# meiva:note
# An agent's flat overlay routes each key to its single owning plugin; framework-scope keys stay with base/config/agent_pins.py. A key whose owner is unknown or ambiguous is dropped rather than raised, matching the framework pins reader's drift tolerance: the stored map was validated against a schema that may no longer exist, and a read must not be stricter than the boot that would consume it. Absent and empty overlays resolve to no pins.
# meiva:link base/packages/plugins/docs/plugin-config/config-view.meiva.okf.md
def resolve_agent_plugin_pins(
    config_overlay: Mapping[str, Any] | None,
    configs: Mapping[str, BaseModel],
) -> dict[str, dict[str, Any]]:
    if not config_overlay:
        return {}
    from base.config import field_names

    framework = field_names()
    pins: dict[str, dict[str, Any]] = {}
    for key, value in config_overlay.items():
        if key in framework:
            continue
        owners = [p for p, config in configs.items() if key in type(config).model_fields]
        if len(owners) != 1:
            continue
        pins.setdefault(owners[0], {})[key] = value
    return pins


# meiva:note
# A missing plugin cluster projection raises InvalidConfigData only when the plugin declares cluster fields and the unit expects a fetch: not config_source_is_local() and should_fetch_from_gateway(). A unit whose config source is local composes from its own authority image - for the gateway-capable unit that owns its image, a missing packet is its normal state (task #5234). A present projection must equal the declared cluster fields exactly (SchemaDriftError) and the composed model must validate (InvalidConfigData); a local-source unit keeps its own values.
# meiva:link base/packages/plugins/docs/plugin-config/config-view.meiva.okf.md
def with_cluster_policy[C: BaseModel](plugin: str, config: C) -> C:
    from base.config import schema_extra
    from base.host.env.bootstrap import (
        cluster_plugin_config_values,
        config_source_is_local,
        should_fetch_from_gateway,
    )
    from base.host.env.dotenv_file import process_env_file, read_env_aliases
    from base.host.env.registry import process_environment

    # meiva:note
    # TODO(config-boot values): the plugin config image is bound deep under the SDK install; its
    # meiva:note
    # root hands the boot facts (environment, `.env` values) down once that install takes them.
    env = process_environment()
    env_file = read_env_aliases(process_env_file())
    cluster_fields = {
        name
        for name, info in type(config).model_fields.items()
        if schema_extra(info).get("scope") in {"cluster-pinned", "cluster-default"}
    }
    projection = cluster_plugin_config_values(env, plugin)
    if projection is None:
        if (
            cluster_fields
            and not config_source_is_local(env, env_file=env_file)
            and should_fetch_from_gateway(env, env_file=env_file)
        ):
            raise InvalidConfigData(f"bootstrap lacks cluster config for plugin {plugin!r}")
        return config
    if set(projection) != cluster_fields:
        raise SchemaDriftError(
            f"plugin {plugin!r} bootstrap cluster fields differ from declaration"
        )
    try:
        composed = type(config).model_validate({**config.model_dump(), **projection})
    except ValidationError as exc:
        raise InvalidConfigData(f"invalid cluster config for plugin {plugin!r}: {exc}") from exc
    return config if config_source_is_local(env, env_file=env_file) else composed


def overlay_config_classes(
    configs: Mapping[str, BaseModel] | None = None,
) -> dict[str, type[BaseModel]]:
    installed = enable_config.installed_plugin_dirs()
    config = enable_config.load_for_runtime(set(installed))
    enabled = {
        name: plugin_dir
        for name, plugin_dir in installed.items()
        if name in config.plugins and config.plugins[name].enabled
    }
    return {
        **enabled_config_classes(enabled),
        **{plugin: type(instance) for plugin, instance in (configs or {}).items()},
    }


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
def read_service_config[C: BaseModel](plugin: str, cls: type[C]) -> C | None:
    from base.host.env.bootstrap import service_plugin_config_packet
    from base.host.env.registry import process_environment

    packet = service_plugin_config_packet(process_environment())
    if packet is None:
        return None
    try:
        data: object = json.loads(packet)
    except json.JSONDecodeError as exc:
        raise InvalidConfigData(f"invalid service plugin config JSON: {exc}") from exc
    if not _is_object_dict(data):
        raise InvalidConfigData("service plugin config requires a JSON object")
    if set(data) != {"plugin", "config"}:
        raise InvalidConfigData("service plugin config requires exactly plugin and config")
    if data["plugin"] != plugin:
        raise InvalidConfigData(
            f"service plugin config names {data['plugin']!r}, expected {plugin!r}"
        )
    values = data["config"]
    if not _is_object_dict(values) or set(values) != set(cls.model_fields):
        raise SchemaDriftError(
            f"service plugin config {plugin!r} field set differs from declaration"
        )
    try:
        return cls.model_validate(values)
    except ValidationError as exc:
        raise InvalidConfigData(f"invalid service plugin config {plugin!r}: {exc}") from exc


def read_plugin_config[C: BaseModel](plugin: str, cls: type[C], config_path: Path) -> C:
    return with_cluster_policy(plugin, read_config_image(cls, config_path))


def plugin_bootstrap_config() -> str:
    from base.config import schema_extra

    payload: dict[str, dict[str, object]] = {}
    for plugin, plugin_dir in sorted(enable_config.discover_plugins().items()):
        cls = declared_config_class(plugin, plugin_dir)
        if cls is None:
            continue
        fields: list[str] = []
        for name, info in cls.model_fields.items():
            extra = schema_extra(info)
            if extra.get("scope") not in {
                "cluster-pinned",
                "cluster-default",
            }:
                continue
            if extra.get("sensitive"):
                raise ValueError(f"plugin cluster config {plugin}.{name} may not carry secrets")
            fields.append(name)
        if fields:
            config = read_config_image(cls, disk_image_path(plugin))
            values = config.model_dump(mode="json")
            payload[plugin] = {name: values[name] for name in fields}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
