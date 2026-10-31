---
type: doc
title: Agent State
description: "Meiva agent's LangGraph conversation, lifecycle, takeover receipt, and plugin state channels."
tags: []
---

# Agent State

## What it is

Meiva agent's LangGraph state management system. Base `BaseAgentState` carries conversation and lifecycle channels, plus nested `compact`, `circuit`, `attach`, `memory`, `context_reset`, `capabilities`, and `skill_recall` state. Plugins declare whole Pydantic BaseModel chunks in `PluginContributions.state`, and the framework merges them into `AgentState`.

`impersonation_introduced` retains the native conversation's first-takeover
explanation receipt across compaction; context establishment restores that
standing explanation only after a takeover has occurred.

`impersonation_applied` records the external lease and plugin-log version applied
in the same checkpoint as its delta; recovery uses it to avoid repeating reducers
after an acknowledgement failure. See [[base/agents/impersonation/docs/impersonation.meiva.okf.md]].

## Core Responsibilities

- **BaseAgentState**: `messages` (delta channel since the 2026-09-14 write switch — guarded reducer pair `guarded_delta_reducer` / `guarded_add_messages`), lifecycle and turn flags (including `non_human_wake: bool = False`, resolved in `claim` from inbound sender polarity, sticky `False` once any human inbound participates in the active run, preserved across mid-run compaction/cancel/restart-resume, and reset to `False` when parking idle), takeover receipts. Nested last-value channels are `compact` (`CompactState`: version, reminder marks, understanding-chunk cut — [[base/agents/history/hierarchy/docs/chunks.meiva.okf.md]]) / `circuit` (`CircuitState`) / `attach` (`AttachState`) / `context_reset` (`ContextReset`) / `capabilities` (`CapabilitiesState`) / `skill_recall` (`SkillRecallState`, beside the `skill_reads` list); `memory` (`MemoryState`) has a per-window union reducer. `agent/state.py:_BASE_STATE_FIELDS` derives from `BaseAgentState.model_fields` (`state._BASE_FIELDS`), auto-syncing the plugin-write guard with base fields.
- **Deferred exec notes**: `pending_exec_notes` is a framework-owned last-value list of context/security notes and media awaiting the last result in a multi-call assistant message. Every call commits plugin fields directly through LangGraph; this buffer controls presentation order only. Completion drains it, compaction clears it, and startup/before-LLM repair drains it after fixing interrupted pairing.
- **Plugin state declaration**: `PluginContributions.state` classes, validated by `plugin_state_schema(extensions)`; the plugin keeps a `PluginStateHandle(Cls, plugin)`
  - Field names overlapping with BaseAgentState → treated as modifying base fields, no prefix added, types must exactly match
  - Field names disjoint → automatically prefixed with `<plugin>__<field>`, becoming plugin-private channels
  - Two plugins declare fields with same name and type → fail-fast error, force rename
- **Dynamic class construction**: `build_agent_state(extensions)` creates `AgentState` (subclass of BaseAgentState + all plugin fields) at graph build time; the class carries its own schema (`__plugin_namespace_fields__`, `__plugin_base_declared__`, `__plugin_state_classes__`), so two registries give two independent classes
- **Type safety**: `PluginStateHandle` validates fully; it has a pure host side (`view` / `delta`, for graph hooks) and an exec side (`read` / `update`, for SDK functions in the exec child) — [[agent/docs/plugin-state-handle.meiva.okf.md]]

## Key Dependencies

- [[graph.meiva.okf.md]] — `build_agent_state(extensions)` called at graph build time
- [[hooks.meiva.okf.md]] — graph edge hooks receive and return the whole `state` parameter (via LangGraph reducer); a plugin hook uses the handle's pure `view` / `delta`

## Entry Points

- `agent/state.py:BaseAgentState` — base state class
- `agent/state.py:plugin_state_schema()` — validates the declared plugin state
- `agent/state.py:build_agent_state()` — dynamic class construction
- `agent/state.py:checkpoint_msgpack_allowlist()` — checkpoint serde allowlist (framework saver + eval driver)

## Notes

- Design allows **multiple plugins declaring the same base field** (e.g., messages), because reducers naturally merge — more flexible than "exclusive fields"
- `meiva.state` / `meiva.state_update` exist only inside an exec turn (elsewhere they raise, never None), and hook modules import no `meiva` — [[agent/docs/plugin-state-handle.meiva.okf.md]]
- Prefix mechanism avoids field name conflicts between plugins, no global registry coordination needed
- **Nested sub-state writing**: `compact` and `attach` are last-value channels. `attach.pending` holds resolved path + optional label entries from completed exec calls until claim drains them into one message, then clears it; duplicate paths retain their first position and latest label. `memory` is a union-reducer channel, recall hook only writes fresh paths for the current turn; reducer accumulates and deduplicates across turns (see `agent/graph/hooks/compact.py`, `plugins/meiva_memory/plugin.py`)
- **`capabilities.indexed`** is the record of what the rendered `# Capabilities` index actually lists — written by `init_context` when it builds the prompt, advanced by the drift check in `agent/graph/hooks/capabilities.py`. Its `None` default is load-bearing and distinct from an empty set: `None` = a checkpoint written before the field existed, where the drift check adopts the live catalog silently rather than announcing the whole catalog as newly installed. See [[system-prompt.meiva.okf.md|System Prompt]]
- **Checkpoint storage, delta message reads, and msgpack allowlist**: channel-key compatibility across schema nesting, `HistoryPostgresSaver` / `HistoryAsyncPostgresSaver` parent-chain delta reads, and `checkpoint_msgpack_allowlist()` rules live in [[agent/docs/state-checkpoint-storage.meiva.okf.md|Agent State Checkpoint Storage]]
