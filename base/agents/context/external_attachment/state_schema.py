import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, TypeGuard

from langchain_core.messages import AnyMessage
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from base.agents.context.external_attachment.state_channels import (
    AttachEntry,
    AttachState,
    CapabilitiesState,
    CircuitState,
    CompactState,
    ContextReset,
    MemoryState,
    SkillRecallState,
    memory_state_merge,
)
from base.agents.history.checkpoint_serde import STATIC_CHECKPOINT_MSGPACK_TYPES
from base.agents.history.checkpoint_serializer import CheckpointSerializer
from base.agents.messages.guard import guarded_add_messages, guarded_delta_reducer
from base.agents.messages.security_finding import SecurityFindingEntry
from base.packages.plugins.extensions import (
    PLUGIN_WRITABLE_BASE_FIELDS,
    SDK_WRITTEN_BASE_FIELDS,
    ExtensionRegistry,
)

MESSAGES_DELTA_SNAPSHOT_FREQUENCY = 1000
MESSAGES_DELTA_CHANNEL: DeltaChannel[Any] = DeltaChannel(
    guarded_delta_reducer, snapshot_frequency=MESSAGES_DELTA_SNAPSHOT_FREQUENCY
)


def _new_any_messages() -> list[AnyMessage]:
    return []


def _new_security_findings() -> list[SecurityFindingEntry]:
    return []


# meiva:link agent/docs/state.meiva.okf.md
class BaseAgentState(BaseModel):
    messages: Annotated[list[AnyMessage], MESSAGES_DELTA_CHANNEL] = Field(
        default_factory=_new_any_messages
    )
    pending_exec_notes: list[AnyMessage] = Field(default_factory=_new_any_messages)
    security_findings: Annotated[list[SecurityFindingEntry], operator.add] = Field(
        default_factory=_new_security_findings
    )
    skill_reads: Annotated[list[str], operator.add] = Field(default_factory=list)
    halted: bool = False
    interrupted_by_restart: bool = False
    harness_cursor: int = 0
    turn_active: bool = False
    exit_requested: bool = False
    restart_requested: bool = False
    turn_idle: bool = False
    impersonation_introduced: bool = False
    impersonation_handoff_id: str | None = None
    impersonation_applied: dict[str, object] = Field(default_factory=dict)

    compact: CompactState = Field(default_factory=CompactState)
    circuit: CircuitState = Field(default_factory=CircuitState)
    attach: AttachState = Field(default_factory=AttachState)
    memory: Annotated[MemoryState, memory_state_merge] = Field(default_factory=MemoryState)
    context_reset: ContextReset = Field(default_factory=ContextReset)
    capabilities: CapabilitiesState = Field(default_factory=CapabilitiesState)
    skill_recall: SkillRecallState = Field(default_factory=SkillRecallState)


BaseAgentState.__module__ = "agent.state"

_BASE_FIELDS: frozenset[str] = frozenset(BaseAgentState.model_fields.keys())

_StateFieldSpec = tuple[Any, FieldInfo]


@dataclass(frozen=True)
class PluginStateSchema:
    extra_fields: dict[str, _StateFieldSpec]
    namespace_fields: dict[str, set[str]]
    base_declared: frozenset[str]
    classes: frozenset[type[BaseModel]]


_PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = PLUGIN_WRITABLE_BASE_FIELDS
_BASE_STATE_FIELDS: frozenset[str] = _BASE_FIELDS


# meiva:note
# Fails fast when a plugin writes undeclared base fields or unregistered keys to
# `meiva.state_update`. `state_cls` is typed as `type[Any]` because dynamically built
# `AgentState` subclasses are passed alongside `BaseModel` types.
# meiva:link agent/docs/state.meiva.okf.md
def validate_plugin_state_keys(update: dict[str, Any], state_cls: type[Any]) -> dict[str, Any]:
    if not update:
        return update
    base_clash = set(update) & _BASE_STATE_FIELDS
    base_declared: frozenset[str] = getattr(state_cls, "__plugin_base_declared__", frozenset())
    illegal_base = base_clash - base_declared - SDK_WRITTEN_BASE_FIELDS
    if illegal_base:
        raise ValueError(
            f"plugin wrote undeclared base field to meiva.state_update: {sorted(illegal_base)} — "
            f"framework core keys are managed by the framework every turn; only "
            f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable, and only when declared "
            f"in the plugin's own BaseModel with the exact BaseAgentState annotation. "
            f"Missing prefix typo? Declared: {sorted(base_declared) or '<empty>'}"
        )
    known = set(state_cls.model_fields.keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    unknown = set(update) - known
    if unknown:
        raise ValueError(
            f"plugin wrote unregistered key to meiva.state_update: {sorted(unknown)} — "
            f"fields not declared in a plugin's `state` contribution are silently dropped by the "
            f"LangGraph reducer in Command(update=). Known plugin fields: "
            f"{sorted(known) or '<empty>'}"
        )
    return update


_MESSAGES_REDUCER = object()


def _is_object_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


# meiva:link agent/docs/state.meiva.okf.md
def is_messages_reducer_form(m: object) -> bool:
    if m is add_messages or m is guarded_add_messages:
        return True
    return isinstance(m, DeltaChannel) and m.reducer is guarded_delta_reducer


# meiva:link agent/docs/state.meiva.okf.md
def messages_annotation_key(annotation: object) -> tuple[Any, ...]:
    meta = getattr(annotation, "__metadata__", None)
    if meta:
        origin = getattr(annotation, "__origin__", None)
        return (
            origin,
            *(_MESSAGES_REDUCER if is_messages_reducer_form(m) else m for m in meta),
        )
    return (annotation,)


# meiva:link agent/docs/state.meiva.okf.md
def resolve_reducer(field: FieldInfo) -> Callable[[Any, Any], Any]:
    for m in field.metadata:
        if isinstance(m, DeltaChannel):
            if m.reducer is guarded_delta_reducer:
                return guarded_add_messages
            continue
        if callable(m) and not isinstance(m, type):
            return guarded_add_messages if m is add_messages else m
    return lambda _old, new: new


# meiva:link agent/docs/state.meiva.okf.md
def accumulate_delta(acc: object, new: object, reducer: Callable[[Any, Any], Any]) -> object:
    if is_messages_reducer_form(reducer):
        acc_list: list[Any] = acc if _is_object_list(acc) else [acc]
        new_list: list[Any] = new if _is_object_list(new) else [new]
        return acc_list + new_list
    return reducer(acc, new)


# meiva:link agent/docs/state.meiva.okf.md
def plugin_state_schema(extensions: ExtensionRegistry) -> PluginStateSchema:
    extra_fields: dict[str, _StateFieldSpec] = {}
    namespace_fields: dict[str, set[str]] = {}
    base_declared: set[str] = set()
    classes: set[type[BaseModel]] = set()
    for plugin, cls in extensions.state_classes():
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            raise TypeError(
                f"plugin {plugin!r} declared state {cls!r}, which is not a BaseModel subclass — "
                f"write `class FooState(BaseModel): ...` and declare it in `PluginContributions.state`."
            )
        classes.add(cls)
        namespace_fields.setdefault(plugin, set())
        for name, model_field in cls.model_fields.items():
            if model_field.metadata:
                raw_annotation = Annotated[model_field.annotation, *model_field.metadata]
            else:
                raw_annotation = model_field.annotation

            if name in _BASE_FIELDS:
                if name not in _PLUGIN_WRITABLE_BASE_FIELDS:
                    raise ValueError(
                        f"plugin {plugin!r} declared core state field {name!r} — only "
                        f"{sorted(_PLUGIN_WRITABLE_BASE_FIELDS)} is plugin-writable among the "
                        f"framework core keys; {name!r} is framework-managed every turn "
                        f"(write a private <plugin>__{name} field or contribute via a hook instead)"
                    )
                base_raw = BaseAgentState.__annotations__[name]
                if messages_annotation_key(raw_annotation) != messages_annotation_key(base_raw):
                    raise ValueError(
                        f"plugin {plugin!r} declared base field '{name}' with type "
                        f"{raw_annotation!r} differing from BaseAgentState's {base_raw!r} — "
                        f"declaring a base field is a contract 'plugin will modify this field'; "
                        f"silent type swaps not allowed."
                    )
                base_declared.add(name)
                continue

            prefixed = f"{plugin}__{name}"
            if prefixed in extra_fields:
                existing_annotation, _ = extra_fields[prefixed]
                if existing_annotation != raw_annotation:
                    raise ValueError(
                        f"state field {prefixed!r} type conflict: "
                        f"{existing_annotation!r} vs {raw_annotation!r}"
                    )
            else:
                extra_fields[prefixed] = (raw_annotation, model_field)
            namespace_fields[plugin].add(name)
    return PluginStateSchema(
        extra_fields, namespace_fields, frozenset(base_declared), frozenset(classes)
    )


def _plugin_namespace_view(state: BaseAgentState, plugin: str) -> SimpleNamespace:
    namespaces: dict[str, set[str]] = getattr(type(state), "__plugin_namespace_fields__", {})
    fields = namespaces.get(plugin)
    if fields is None:
        known = sorted(namespaces)
        raise AttributeError(
            f"meiva.state.{plugin} does not exist — plugin {plugin!r} declares no state, "
            f"or plugin name typo. Known plugins: {known or '<empty>'}"
        )
    prefix = f"{plugin}__"
    return SimpleNamespace(**{name: getattr(state, f"{prefix}{name}") for name in fields})


# meiva:link agent/docs/state.meiva.okf.md
def build_agent_state(extensions: ExtensionRegistry) -> type[BaseAgentState]:
    schema = plugin_state_schema(extensions)
    if not schema.extra_fields and not schema.namespace_fields:
        return BaseAgentState

    annotations: dict[str, Any] = {}
    namespace: dict[str, Any] = {
        "__annotations__": annotations,
        "__plugin_namespace_fields__": schema.namespace_fields,
        "__plugin_base_declared__": schema.base_declared,
        "__plugin_state_classes__": schema.classes,
    }
    for name, (annotation, field) in schema.extra_fields.items():
        annotations[name] = annotation
        namespace[name] = field

    def _state_getattr(self: BaseAgentState, item: str) -> SimpleNamespace:
        if item.startswith("_"):
            raise AttributeError(item)
        return _plugin_namespace_view(self, item)

    namespace["__getattr__"] = _state_getattr

    cls = type("AgentState", (BaseAgentState,), namespace)
    cls.__module__ = "agent.state"
    return cls


AgentState = BaseAgentState


# meiva:link agent/docs/state.meiva.okf.md
def checkpoint_msgpack_allowlist(
    plugin_state_classes: Iterable[type[BaseModel]] = (),
) -> frozenset[tuple[str, str]]:
    entries: set[tuple[str, str]] = set(STATIC_CHECKPOINT_MSGPACK_TYPES)
    for cls in plugin_state_classes:
        entries.add((cls.__module__, cls.__name__))
    return frozenset(entries)


# meiva:link agent/docs/state.meiva.okf.md
def build_checkpoint_serde(
    plugin_state_classes: Iterable[type[BaseModel]] = (),
) -> JsonPlusSerializer:
    return CheckpointSerializer(
        allowed_msgpack_modules=checkpoint_msgpack_allowlist(plugin_state_classes)
    )


__all__ = [
    "MESSAGES_DELTA_CHANNEL",
    "MESSAGES_DELTA_SNAPSHOT_FREQUENCY",
    "AgentState",
    "AttachEntry",
    "AttachState",
    "BaseAgentState",
    "CapabilitiesState",
    "CircuitState",
    "CompactState",
    "ContextReset",
    "MemoryState",
    "PluginStateSchema",
    "SkillRecallState",
    "accumulate_delta",
    "build_agent_state",
    "build_checkpoint_serde",
    "checkpoint_msgpack_allowlist",
    "guarded_add_messages",
    "guarded_delta_reducer",
    "is_messages_reducer_form",
    "memory_state_merge",
    "messages_annotation_key",
    "plugin_state_schema",
    "resolve_reducer",
    "validate_plugin_state_keys",
]
