import json
import math
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from base.packages.plugin_config_contracts import ConfigImage, PluginConfigChangedError
from base.packages.plugins import (
    all_plugin_configs,
    config_from_image,
    get_plugin_config,
    is_per_agent_field,
    process_plugin_config,
    service_config_packet,
)
from base.packages.plugins.config_face import (
    disk_image_path,
    read_config_image,
    write_default_disk_image,
)
from base.packages.plugins.config_view import (
    overlay_config_classes,
    plugin_bootstrap_config,
    read_plugin_config,
    read_service_config,
    with_cluster_policy,
)
from base.packages.plugins.plugin_errors import (
    DuplicateRegistration,
    InvalidConfigData,
    InvalidConfigOverlay,
    SchemaDriftError,
)

__all__ = [
    "DuplicateRegistration",
    "InvalidConfigData",
    "InvalidConfigOverlay",
    "OverlayPlan",
    "SchemaDriftError",
    "all_plugin_configs",
    "bind_plugin_config",
    "config_from_image",
    "disk_image_path",
    "get_plugin_config",
    "is_per_agent_field",
    "merge_disk_image_schema",
    "overlay_config_classes",
    "plan_config_overlay",
    "plugin_bootstrap_config",
    "process_plugin_config",
    "read_config_image",
    "read_plugin_config",
    "read_service_config",
    "resolve_overlay_targets",
    "service_config_packet",
    "validate_config_overlay",
    "validate_config_overlay_shape",
    "write_default_disk_image",
]


def bind_plugin_config(owner: ConfigImage, configs: dict[str, BaseModel]) -> Callable[[], None]:
    plugin, cls = owner.name, owner.cls
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(
            f"plugin {plugin!r} declared config {cls!r}, which is not a BaseModel subclass — "
            f"write `class FooConfig(BaseModel): ...` and declare it in `PluginContributions.config`."
        )
    if plugin in configs:
        raise DuplicateRegistration(
            f"plugin {plugin!r} already has a bound config ({type(configs[plugin]).__name__})"
        )
    instance = _instantiate_from_disk(owner)
    configs[plugin] = instance

    def undo() -> None:
        configs.pop(plugin, None)

    return undo


def _instantiate_from_disk(owner: ConfigImage) -> BaseModel:
    try:
        content = owner.read()
    except FileNotFoundError:
        initial = owner.cls()
        missing = True
    else:
        initial = config_from_image(owner.cls, content, owner.path)
        missing = False
    with_cluster_policy(owner.name, initial)
    if missing:
        with suppress(PluginConfigChangedError):
            owner.create_default()
    return with_cluster_policy(owner.name, config_from_image(owner.cls, owner.read(), owner.path))


def _is_str_dict(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def _reconcile_config_image[C: BaseModel](
    cls: type[C], content: str | None, config_path: Path
) -> tuple[C, set[str], set[str]]:
    if content is None:
        return cls(), set(cls.model_fields), set()
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidConfigData(f"plugin disk image JSON malformed ({config_path}): {exc}") from exc
    if not _is_str_dict(data):
        raise InvalidConfigData(f"plugin config disk image must be a JSON object ({config_path})")
    disk_keys = set(data)
    cls_keys = set(cls.model_fields)
    defaults = cls().model_dump(mode="json")
    merged = {field: data[field] if field in disk_keys else defaults[field] for field in cls_keys}
    try:
        candidate = cls.model_validate(merged)
    except ValidationError as exc:
        raise InvalidConfigData(
            f"plugin disk image post-merge Pydantic validation failed ({config_path}): {exc}"
        ) from exc
    return candidate, cls_keys - disk_keys, disk_keys - cls_keys


# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
def merge_disk_image_schema(owner: ConfigImage) -> tuple[set[str], set[str]]:
    cls, config_path = owner.cls, owner.path

    try:
        captured = owner.read()
    except FileNotFoundError:
        owner.create_default()
        return set(cls.model_fields.keys()), set()
    candidate, added, removed = _reconcile_config_image(cls, captured, config_path)
    if not added and not removed:
        return set(), set()

    from base.packages.plugin_config_images import (
        image_revision,
        write_config_image,
    )

    write_config_image(
        owner,
        candidate,
        expected_digest=image_revision(captured.encode()),
    )
    return added, removed


def _schema_extra(info: FieldInfo) -> dict[str, Any]:
    extra = info.json_schema_extra
    return extra if _is_str_dict(extra) else {}


def _per_agent_fields_for(cls: type[BaseModel]) -> set[str]:
    out: set[str] = set()
    for name, info in cls.model_fields.items():
        if _schema_extra(info).get("per_agent") is True:
            out.add(name)
    return out


# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
def _framework_field_tables() -> tuple[set[str], set[str], Callable[[str], str]]:
    from base.host.env.config_lite_table import FIELD_DOMAINS, PER_AGENT_FIELDS

    return set(PER_AGENT_FIELDS), set(FIELD_DOMAINS), FIELD_DOMAINS.__getitem__


def resolve_overlay_targets(
    overlay: dict[str, object], configs: Mapping[str, BaseModel] | None = None
) -> dict[str, tuple[str | None, str]]:
    framework_per_agent, framework_all, _ = _framework_field_tables()

    plugin_classes = overlay_config_classes(configs)
    out: dict[str, tuple[str | None, str]] = {}
    for key in overlay:
        matches: list[tuple[str | None, str]] = []
        if key in framework_all:
            matches.append((None, key))
        for plugin, cls in plugin_classes.items():
            if key in cls.model_fields:
                matches.append((plugin, key))

        if not matches:
            raise InvalidConfigOverlay(
                f"overlay key {key!r} is not in framework Settings nor any plugin "
                f"Config — typo? Known plugins: {sorted(plugin_classes)}"
            )
        if len(matches) > 1:
            owners = ", ".join(f"{p or 'framework'}.{f}" for p, f in matches)
            raise InvalidConfigOverlay(
                f"overlay key {key!r} collides across multiple owners: {owners} — current PR-E "
                f"does not support explicit namespace; rename one or wait for namespace support."
            )
        plugin, field = matches[0]
        if plugin is None:
            if field not in framework_per_agent:
                raise InvalidConfigOverlay(
                    f"overlay key {key!r} is a framework Settings field but not marked "
                    f"per_agent=True — cluster-consistent fields (db_url etc.) do not allow "
                    f"per-agent override; if truly needed change framework Settings and plugin Config metadata."
                )
        else:
            cls = plugin_classes[plugin]
            if field not in _per_agent_fields_for(cls):
                raise InvalidConfigOverlay(
                    f"overlay key {key!r} is in plugin {plugin!r} but not marked "
                    f"per_agent=True — add json_schema_extra={{'per_agent': True}} to the "
                    f"default_config.py field to enable overlay."
                )
        out[key] = (plugin, field)
    return out


def _validate_model_membership(
    value: object, models: Mapping[str, object], *, kind: str = "model"
) -> str | None:
    if isinstance(value, str) and value in models:
        return None
    valid_models = ", ".join(sorted(models))
    return f"value {value!r} is not a registered {kind}; valid {kind}s: {valid_models}"


def _validate_reasoning_effort_range(value: object) -> str | None:
    from base.lm.effort import EFFORT_VOCAB

    if value is None:
        return None
    valid_values = ("", *EFFORT_VOCAB)
    if isinstance(value, str) and value in valid_values:
        return None
    rendered_values = ", ".join(repr(candidate) for candidate in valid_values)
    return f"value {value!r} is not valid; valid values: {rendered_values}"


def _range_validator(
    *,
    gt: float | None = None,
    ge: float | None = None,
    le: float | None = None,
) -> Callable[[object], str | None]:

    def check(value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return f"value {value!r} must be a finite number (NaN/Inf rejected)"
        if gt is not None and value <= gt:
            return f"value {value!r} must be greater than {gt}"
        if ge is not None and value < ge:
            return f"value {value!r} must be at least {ge}"
        if le is not None and value > le:
            return f"value {value!r} must be at most {le}"
        return None

    return check


# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
_FRAMEWORK_RANGE_VALIDATORS: dict[str, Callable[[object], str | None]] = {
    "reasoning_effort": _validate_reasoning_effort_range,
    "heartbeat_pause_max_seconds": _range_validator(gt=0),
    "llm_stream_inter_chunk_timeout_seconds": _range_validator(gt=0),
    "llm_stream_total_timeout_seconds": _range_validator(gt=0),
    "llm_stream_ttft_timeout_seconds": _range_validator(gt=0),
    "memory_recall_deadline_seconds": _range_validator(gt=0),
    "skill_recall_deadline_seconds": _range_validator(gt=0),
    "memory_write_check_deadline_seconds": _range_validator(gt=0),
    "memory_recall_inject_threshold": _range_validator(ge=0, le=1),
    "memory_write_dedup_threshold": _range_validator(ge=0, le=1),
    "auto_compact_fraction": _range_validator(gt=0, le=1),
    "compact_reminder_fraction": _range_validator(gt=0, le=1),
    "skill_recall_threshold": _range_validator(gt=0, le=1),
    "skill_recall_peer_threshold": _range_validator(gt=0, le=1),
    "skill_recall_peer_gate_max_chars": _range_validator(gt=0),
    "skill_recall_peer_gate_deadline_seconds": _range_validator(gt=0),
    "auto_compact_ceiling_tokens": _range_validator(ge=0),
    "claude_thinking_budget_tokens": _range_validator(ge=0),
    "history_dump_keep": _range_validator(ge=0),
    "memory_recall_inject_max": _range_validator(ge=0),
    "skill_recall_max": _range_validator(ge=0),
    "to_user_soft_limit_words": _range_validator(gt=0),
}


def _validate_framework_overlay_ranges(updates: dict[str, object]) -> None:
    for field, value in updates.items():
        validator = _FRAMEWORK_RANGE_VALIDATORS.get(field)
        if validator is None:
            continue
        error = validator(value)
        if error is not None:
            raise InvalidConfigOverlay(f"overlay key {field!r} {error}")


# meiva:link base/packages/plugins/docs/plugin-config/plugin-config.meiva.okf.md
def validate_config_overlay_shape(
    overlay: dict[str, object], configs: Mapping[str, BaseModel] | None = None
) -> None:
    from base.config.service_read import domain_model_classes

    targets = resolve_overlay_targets(overlay, configs)
    grouped: dict[str | None, dict[str, object]] = {}
    for key, (plugin, field) in targets.items():
        grouped.setdefault(plugin, {})[field] = overlay[key]

    for plugin, updates in grouped.items():
        try:
            if plugin is None:
                _, _, domain_of = _framework_field_tables()
                by_domain: dict[str, dict[str, object]] = {}
                for f, v in updates.items():
                    by_domain.setdefault(domain_of(f), {})[f] = v
                for dom, upd in by_domain.items():
                    cls = domain_model_classes()[dom]
                    sub = cls()
                    cls.model_validate({**sub.model_dump(), **upd})
            else:
                cls = overlay_config_classes(configs)[plugin]
                held = None if configs is None else configs.get(plugin)
                current = (held if held is not None else cls()).model_dump()
                cls(**{**current, **updates})
        except ValidationError as e:
            owner = "framework" if plugin is None else f"plugin {plugin!r}"
            raise InvalidConfigOverlay(
                f"overlay field type validation failed ({owner}): {e}"
            ) from e

    if None in grouped:
        _validate_framework_overlay_ranges(grouped[None])


_DECISION_MODEL_FIELDS = ("skill_recall_model",)


def validate_config_overlay(
    overlay: dict[str, object],
    configs: Mapping[str, BaseModel] | None = None,
    *,
    models: Mapping[str, object],
    decision_models: Mapping[str, object] | None = None,
) -> None:
    validate_config_overlay_shape(overlay, configs)
    if "llm_model" in overlay:
        error = _validate_model_membership(overlay["llm_model"], models)
        if error is not None:
            raise InvalidConfigOverlay(f"overlay key 'llm_model' {error}")
    for field in _DECISION_MODEL_FIELDS:
        if field in overlay:
            if decision_models is None:
                raise InvalidConfigOverlay(
                    f"overlay key {field!r} names a decision model, but the caller supplied "
                    "no decision-model table to check it against"
                )
            error = _validate_model_membership(
                overlay[field], decision_models, kind="decision model"
            )
            if error is not None:
                raise InvalidConfigOverlay(f"overlay key {field!r} {error}")


# meiva:note
# What an overlay does, as a plan: the framework fields it assigns, in overlay order, and the
# plugin configs after its plugin fields (a new instance per touched plugin, the others unchanged).
#
# Nothing is mutated here. The caller's shell sets each framework (field, value) on the
# configuration owner it holds and raises `InvalidConfigOverlay` for a failing field; plugin
# configs are only planned when `configs` is given.
@dataclass(frozen=True)
class OverlayPlan:
    framework_fields: tuple[tuple[str, object], ...]
    plugin_configs: dict[str, BaseModel]


def plan_config_overlay(
    overlay: dict[str, object], configs: Mapping[str, BaseModel] | None = None
) -> OverlayPlan:
    targets = resolve_overlay_targets(overlay, configs)
    framework = tuple(
        (field, overlay[key]) for key, (plugin, field) in targets.items() if plugin is None
    )
    resolved = dict(configs or {})
    grouped: dict[str, dict[str, object]] = {}
    for key, (plugin, field) in targets.items():
        if plugin is not None and configs is not None:
            grouped.setdefault(plugin, {})[field] = overlay[key]
    for plugin, updates in grouped.items():
        instance = resolved[plugin]
        try:
            resolved[plugin] = type(instance)(**{**instance.model_dump(), **updates})
        except ValidationError as e:
            raise InvalidConfigOverlay(
                f"overlay field type validation failed (plugin {plugin!r}): {e}"
            ) from e
    return OverlayPlan(framework, resolved)
