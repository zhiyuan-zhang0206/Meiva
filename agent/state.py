# meiva:note
# LangGraph state — supports whole-class plugin state registration.
#
# BaseAgentState (framework layer, static):
#     messages         — cross-turn LLM history (guarded add_messages reducer: append-only invariant, task #1256)
#     halted           — whether the current turn has ended
#     interrupted_by_restart — a restart ended the model call or code run in flight, so `halted` marks an unfinished turn
#     turn_active      — the current graph invocation is mid-turn (claim's turn boundary)
#     non_human_wake   — current run woke solely on non-human inbounds (assistant text defaults internal unless prefixed)
#     exit_requested   — claim's END means process exit, not just turn over
#     turn_idle        — hosted mode: claim found nothing and did not park, so the host ends the turn task
#     compact          — nested compaction bookkeeping (CompactState, agent/state_channels.py)
#     circuit          — nested heartbeat circuit-breaker state (CircuitState, agent/state_channels.py)
#     attach           — nested next-turn attachment queue (AttachState, agent/state_channels.py)
#     memory           — nested passive-recall bookkeeping (MemoryState, agent/state_channels.py)
#     capabilities     — nested capability-index snapshot (CapabilitiesState, agent/state_channels.py)
#     context_reset    — nested pending-context-reset bookkeeping (ContextReset, agent/state_channels.py)
#
# A plugin declares an entire BaseModel in its `contribute()` (`PluginContributions.state`) and
# keeps a `PluginStateHandle[Cls]` (built from the class and its plugin name) for typed read/write;
# framework dispatches by field name:
#
# - Field name ∈ BaseAgentState (messages / halted): treated as "plugin
#   declares it will modify this base field"; no prefix added; type must
#   match base exactly (including Annotated reducer), otherwise raise.
#   Multiple plugins declaring the same base field all modify the same
#   channel; reducer naturally merges.
# - Field name ∉ base: auto-prefixed `<plugin>__<field>` into the merged
#   AgentState; plugin-private channel.
#
# `build_agent_state(extensions)` dynamically creates AgentState (BaseAgentState
# subclass + plugin-declared fields) at graph build time. A plugin reaches its state two
# ways, one per side of the exec process boundary, both through its `PluginStateHandle`:
#
# - host side (graph hooks, in the agent process): `handle.view(state)` builds the typed
#   snapshot from the graph `state` argument, and `handle.delta({...})` turns a plugin-local
#   update into the prefixed update dict the hook returns for LangGraph's reducer. Pure
#   functions of the class and the state — no SDK slot, no `meiva`.
# - exec side (SDK functions running in the exec child): `handle.read()` / `handle.update()`
#   work on the exec turn's slot (`meiva.state` / `meiva.state_update`, framework-internal).
#
# Usage (in a plugin's agent_runtime.py):
#
#     from agent.state import PluginStateHandle
#     from base.packages.plugins.extensions import PluginContributions
#     from pydantic import BaseModel, Field
#     from typing import Annotated
#
#     def _set_union(old: set[str], new: set[str]) -> set[str]:
#         return old | new
#
#     class MyPluginState(BaseModel):
#         counter: int = Field(default=0)
#         seen: Annotated[set[str], _set_union] = Field(default_factory=set)
#
#     state_handle = PluginStateHandle(MyPluginState, "my_plugin")
#     # exec side: state_handle.read() -> MyPluginState; state_handle.update({"counter": 1})
#     # host side, in a hook(state, runtime, config): state_handle.view(state) -> MyPluginState,
#     #   return state_handle.delta({"counter": 1})  # -> {"my_plugin__counter": 1}
#
#     def contribute() -> PluginContributions:
#         return PluginContributions(state=(MyPluginState,))


from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from agent.state_channels import AttachEntry as _AttachEntry
from agent.state_channels import (
    AttachState,
    CapabilitiesState,
    CircuitState,
    CompactState,
    ContextReset,
    MemoryState,
    SkillRecallState,
    memory_state_merge,
)
from base.agents.context.external_attachment.state_schema import (
    MESSAGES_DELTA_CHANNEL,
    MESSAGES_DELTA_SNAPSHOT_FREQUENCY,
    AgentState,
    BaseAgentState,
    PluginStateSchema,
    accumulate_delta,
    build_agent_state,
    build_checkpoint_serde,
    checkpoint_msgpack_allowlist,
    guarded_add_messages,
    guarded_delta_reducer,
    is_messages_reducer_form,
    messages_annotation_key,
    plugin_state_schema,
    resolve_reducer,
    validate_plugin_state_keys,
)

AttachEntry = _AttachEntry
_BASE_FIELDS: frozenset[str] = frozenset(BaseAgentState.model_fields.keys())
_BASE_STATE_FIELDS: frozenset[str] = _BASE_FIELDS


class PluginStateHandle[T: BaseModel]:
    """Typed read/write handle for plugin state. Built from a declared state class and its plugin's name.

    Two sides, because the plugin's state is touched from two processes:

    Host side — a graph hook runs in the agent process and was handed the graph `state`;
    it never touches the SDK's exec slot (which does not exist there):

        handle.view(state) -> T                     (typed snapshot of the graph state)
        handle.delta({"field": value}) -> dict      (the update dict the hook returns)

    Both are pure; LangGraph's reducer applies the returned dict.

    Exec side — SDK functions running inside the exec child (`meiva.cwd.set`, the read wrap)
    work on the turn's slot. API shape matches LangGraph Command(update=dict):

        handle.read() -> T                          (typed snapshot)
        handle.update({"field": delta_value})       (validated + reducer-merged)

    Within the same turn, update() is immediately visible: handle
    simultaneously mutates the `meiva.state` working copy + accumulates the
    raw delta into `meiva.state_update`. At turn end, exec_node merges
    state_update into Command(update=...), and the LangGraph reducer runs
    again on the commit side (with the accumulated delta as reducer input,
    producing the same result as the working copy — reducers must be
    batching-invariant, i.e. merging deltas then applying once must equal
    applying them in sequence; `accumulate_delta` implements the merge).
    Outside an exec turn `read()` / `update()` raise `PluginStateOutsideTurnError`.

    Base field reuse: if a plugin BaseModel declares a field with the same
    name as one in BaseAgentState (types matching exactly, validated at
    plugin_state_schema), the handle routes those fields to the
    base channel (no prefix). A single update dict can mix base / plugin
    fields; the handle internally dispatches by _BASE_FIELDS.
    """

    def __init__(self, cls: type[T], plugin_name: str | None) -> None:
        self._cls = cls
        self._plugin_name = plugin_name
        # Field name → actual LangGraph channel key.
        #   base field: bare name, shares BaseAgentState channel.
        #   plugin field: <plugin>__<field>; when plugin_name=None, falls back
        #   to bare name (supports test fixtures directly calling PluginStateHandle
        #   — normal declared-state path always passes name).
        self._channel_keys: dict[str, str] = {}
        for name in cls.model_fields:
            if name in _BASE_FIELDS:
                self._channel_keys[name] = name
            else:
                self._channel_keys[name] = f"{plugin_name}__{name}" if plugin_name else name
        # Reducer per field. Base fields must declare exactly as BaseAgentState
        # (including Annotated reducer; plugin_state_schema already
        # validates), so extracting what the plugin wrote gives the same
        # reducer as base; no need to look up base separately.
        self._reducers: dict[str, Callable[[Any, Any], Any]] = {
            name: resolve_reducer(field) for name, field in cls.model_fields.items()
        }

    def view(self, state: object) -> T:
        """Typed snapshot of this plugin's fields in a graph `state` — a hook's argument.

        The graph already validated `state`, so the fields are taken as they stand
        (`model_construct`): no second validation, no copy of the message history a plugin
        may declare. Pure: reads only `state`.
        """
        return self._cls.model_construct(
            **{f: getattr(state, self._channel_keys[f]) for f in self._cls.model_fields}
        )

    def delta(self, update: Mapping[str, Any]) -> dict[str, Any]:
        """The graph-state update dict for plugin-local `update` — what a hook returns.

        Keys are plugin-local field names (no prefix); the result is keyed by the actual
        channels, ready for LangGraph's reducer. Pure.

        Raises:
            ValueError: `update` has a key outside the BaseModel schema (plugin author typo).
        """
        out: dict[str, Any] = {}
        for field, value in update.items():
            if field not in self._cls.model_fields:
                raise ValueError(
                    f"PluginStateHandle[{self._cls.__name__}].delta: unknown field "
                    f"{field!r} (schema declares {sorted(self._cls.model_fields)})"
                )
            out[self._channel_keys[field]] = value
        return out

    def read(self) -> T:
        """Current exec-turn snapshot. Reflects all `update()` calls in same turn.

        Raises:
            PluginStateOutsideTurnError: not inside an exec turn (`meiva.state` is unbound).
        """
        import meiva  # lazy import: avoid circular (meiva imports agent.state via plugin loading)
        from meiva.sdk_surface.agent_identity import validate_external_identity

        validate_external_identity()

        slot = meiva.state
        return self._cls.model_validate(
            {f: getattr(slot, self._channel_keys[f]) for f in self._cls.model_fields}
        )

    def update(self, delta: dict[str, Any]) -> None:
        """Apply field updates inside an exec turn. Same API shape as LangGraph Command(update=dict).

        Each field's reducer merges `current ⊕ delta`; the result is written
        to the `meiva.state` working copy (immediately visible to `read()`
        within this turn) + raw delta accumulated into `meiva.state_update`
        (at turn end, LangGraph commits and the reducer runs again to get
        the final value).

        Keys use plugin-local field names (no prefix). The handle internally
        dispatches base/plugin to the correct channel.

        Raises:
            PluginStateOutsideTurnError: not inside an exec turn (`meiva.state` is unbound).
            TypeError: the exec code replaced `meiva.state_update` with a non-dict.
            ValueError: delta contains a key outside the BaseModel schema (plugin author typo).
        """
        import meiva
        from meiva.sdk_surface.agent_identity import validate_external_identity

        validate_external_identity()

        slot = meiva.state
        accumulated = meiva.state_update
        if not isinstance(accumulated, dict):
            raise TypeError(
                f"meiva.state_update must stay a dict, got {type(accumulated).__name__} "
                f"(PluginStateHandle[{self._cls.__name__}].update)"
            )
        for field, new in delta.items():
            if field not in self._cls.model_fields:
                raise ValueError(
                    f"PluginStateHandle[{self._cls.__name__}].update: unknown field "
                    f"{field!r} (schema declares {sorted(self._cls.model_fields)})"
                )
            channel_key = self._channel_keys[field]
            current = getattr(slot, channel_key)
            merged = self._reducers[field](current, new)
            setattr(slot, channel_key, merged)  # working copy synchronously visible
            # Accumulate the raw delta into meiva.state_update: overwriting it dropped every
            # earlier delta to a reducer field in one turn (two `update({"seen": ...})` calls
            # committed only the last while read() saw both). A last-value field's reducer is
            # overwrite, so it still collapses to the latest delta (see `accumulate_delta`).
            if channel_key in accumulated:
                accumulated[channel_key] = accumulate_delta(
                    accumulated[channel_key], new, self._reducers[field]
                )
            else:
                accumulated[channel_key] = new


def compact_version() -> int:
    """The exec turn snapshot's `compact.version` — the built-in compaction counter.

    A built-in channel no plugin declares, so it is not reachable through a
    `PluginStateHandle`; exec-side code that resets per-compaction bookkeeping (injection
    dedup) reads it here instead of reaching into `meiva.state`.

    Raises:
        PluginStateOutsideTurnError: not inside an exec turn (`meiva.state` is unbound).
    """
    import meiva
    from meiva.sdk_surface.agent_identity import validate_external_identity

    validate_external_identity()

    return meiva.state.compact.version


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
    "PluginStateHandle",
    "PluginStateSchema",
    "SkillRecallState",
    "accumulate_delta",
    "build_agent_state",
    "build_checkpoint_serde",
    "checkpoint_msgpack_allowlist",
    "compact_version",
    "guarded_add_messages",
    "guarded_delta_reducer",
    "is_messages_reducer_form",
    "memory_state_merge",
    "messages_annotation_key",
    "plugin_state_schema",
    "resolve_reducer",
    "validate_plugin_state_keys",
]
