"""`plugin_state_schema` / `build_agent_state(extensions)`: plugin state is a declaration, validated
and turned into a class per registry — nothing process-global is mutated by a declaration.

The core-key rejection (every BaseAgentState field other than `messages`) is covered next to the
exec-node tests in `test_state_slot.py`.
"""

import sys
import types
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Annotated, TypeGuard

import pytest
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

import agent.state as agent_state
from agent.state import (
    BaseAgentState,
    PluginStateHandle,
    build_agent_state,
    checkpoint_msgpack_allowlist,
    plugin_state_schema,
    validate_plugin_state_keys,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_mapping, object_of
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions
from base.packages.plugins.loading.registry import loaded_state_classes


def _declared(state_cls: type[BaseAgentState], name: str) -> object:
    """A class attribute `build_agent_state` stamps on the class it builds."""
    return object_mapping(state_cls.__dict__)[name]


def _declared_namespaces(state_cls: type[BaseAgentState]) -> set[str]:
    declared = _declared(state_cls, "__plugin_namespace_fields__")
    assert is_object_dict(declared)
    return set(declared)


def _is_object_frozenset(value: object) -> TypeGuard[frozenset[object]]:
    return isinstance(value, frozenset)


def _declared_classes(state_cls: type[BaseAgentState]) -> frozenset[type[BaseModel]]:
    declared = _declared(state_cls, "__plugin_state_classes__")
    assert _is_object_frozenset(declared)
    classes: set[type[BaseModel]] = set()
    for member in declared:
        assert isinstance(member, type)
        assert issubclass(member, BaseModel)
        classes.add(member)
    return frozenset(classes)


def _namespace_values(namespace: object) -> Mapping[str, object]:
    assert isinstance(namespace, SimpleNamespace)
    return object_mapping(vars(namespace))


def _registry(*plugins: tuple[str, type[BaseModel]]) -> ExtensionRegistry:
    return ExtensionRegistry(
        tuple((name, PluginContributions(state=(cls,))) for name, cls in plugins)
    )


class _Private(BaseModel):
    counter: int = 0
    tags: set[str] = Field(default_factory=set)


class _MessagesDeclaring(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    note: str = ""


def test_private_fields_get_the_plugin_prefix_and_the_namespace_view_works() -> None:
    state_cls = build_agent_state(_registry(("demo", _Private)))

    assert {"demo__counter", "demo__tags"} <= set(state_cls.model_fields)
    assert "counter" not in state_cls.model_fields
    state = state_cls(demo__counter=3, demo__tags={"a"})  # pyright: ignore[reportCallIssue]
    ns = _namespace_values(object_of(getattr, state, "demo"))
    assert (ns["counter"], ns["tags"]) == (3, {"a"})
    with pytest.raises(AttributeError, match="demo"):
        _namespace_values(object_of(getattr, state, "other"))


def test_a_registry_without_state_builds_the_base_class() -> None:
    assert build_agent_state(ExtensionRegistry()) is BaseAgentState


def test_a_non_messages_core_field_raises() -> None:
    class _Halting(BaseModel):
        halted: bool = False

    with pytest.raises(ValueError, match="core state field 'halted'"):
        plugin_state_schema(_registry(("demo", _Halting)))


def test_a_mismatched_messages_annotation_raises() -> None:
    class _WrongMessages(BaseModel):
        messages: list[str] = Field(default_factory=list)

    with pytest.raises(ValueError, match="base field 'messages'"):
        plugin_state_schema(_registry(("demo", _WrongMessages)))


def test_a_declared_state_that_is_not_a_basemodel_raises() -> None:
    class _NotAModel:
        counter: int = 0

    registry = ExtensionRegistry(
        (("demo", PluginContributions(state=(_NotAModel,))),)  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(TypeError, match="not a BaseModel subclass"):
        plugin_state_schema(registry)


def test_two_registries_build_independent_classes() -> None:
    first = build_agent_state(_registry(("first", _Private)))
    second = build_agent_state(_registry(("second", _MessagesDeclaring)))

    assert "first__counter" in first.model_fields
    assert not any(name.startswith("first__") for name in second.model_fields)
    assert "second__note" in second.model_fields
    assert not any(name.startswith("second__") for name in first.model_fields)
    assert _declared_namespaces(first) == {"first"}
    assert _declared_namespaces(second) == {"second"}
    assert _declared(first, "__plugin_base_declared__") == frozenset()
    assert _declared(second, "__plugin_base_declared__") == frozenset({"messages"})
    assert _declared_classes(first) == frozenset({_Private})
    assert _declared_classes(second) == frozenset({_MessagesDeclaring})
    # The first class still answers for its own plugin set after the second was built.
    state = first(first__counter=1)  # pyright: ignore[reportCallIssue]
    assert _namespace_values(object_of(getattr, state, "first"))["counter"] == 1


def test_state_key_validation_reads_the_classs_own_declared_base_fields() -> None:
    declaring = build_agent_state(_registry(("demo", _MessagesDeclaring)))
    plain = build_agent_state(_registry(("demo", _Private)))

    assert validate_plugin_state_keys({"messages": []}, declaring) == {"messages": []}
    with pytest.raises(ValueError, match="undeclared base field"):
        validate_plugin_state_keys({"messages": []}, plain)
    # A framework-managed core key is never writable, declared or not.
    with pytest.raises(ValueError, match="undeclared base field"):
        validate_plugin_state_keys({"halted": True}, declaring)
    # A prefixed private field of the class's own plugin passes; a typo does not.
    assert validate_plugin_state_keys({"demo__counter": 1}, plain) == {"demo__counter": 1}
    with pytest.raises(ValueError, match="unregistered key"):
        validate_plugin_state_keys({"demo__typo": 1}, plain)


def test_the_handle_routes_base_fields_bare_and_private_fields_prefixed() -> None:
    handle = PluginStateHandle(_MessagesDeclaring, "demo")

    assert handle._channel_keys == {"messages": "messages", "note": "demo__note"}


def test_the_allowlist_includes_a_plugin_class_only_when_passed_it() -> None:
    entry = (_Private.__module__, _Private.__name__)

    assert entry not in checkpoint_msgpack_allowlist()
    assert entry in checkpoint_msgpack_allowlist((_Private,))
    # The classes a built AgentState carries are what a serializer holding that class gets.
    state_cls = build_agent_state(_registry(("demo", _Private)))
    assert entry in checkpoint_msgpack_allowlist(_declared_classes(state_cls))


def test_building_the_state_class_binds_nothing_into_the_module() -> None:
    assert build_agent_state(_registry(("demo", _Private))) is not agent_state.AgentState
    assert agent_state.AgentState is BaseAgentState


def test_a_registryless_process_allows_the_state_classes_of_its_loaded_faces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _Private not in loaded_state_classes()
    face = types.ModuleType("plugins.demo.agent_runtime")
    face.contribute = lambda: PluginContributions(state=(_Private,))  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setitem(sys.modules, "plugins.demo.agent_runtime", face)

    assert _Private in loaded_state_classes()
