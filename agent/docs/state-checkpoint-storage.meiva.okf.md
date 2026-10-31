---
type: doc
title: Agent State Checkpoint Storage
description: Checkpoint key compatibility, parent-chain delta message reads, and the msgpack serializer allowlist for AgentState.
tags: []
---

# Agent State Checkpoint Storage

How `BaseAgentState` and dynamically merged `AgentState` channels cross LangGraph's Postgres checkpointer (`HistoryPostgresSaver` / `HistoryAsyncPostgresSaver` and `JsonPlusSerializer`). Channel definitions and plugin state merging live in [[agent/docs/state.meiva.okf.md|Agent State]].

## Entry Points

- `agent/state.py:checkpoint_msgpack_allowlist()` — checkpoint serde allowlist (framework saver + eval driver)
- `base/agents/history/checkpoint_postgres_walks.py:HistoryPostgresSaver` / `HistoryAsyncPostgresSaver` — parent-chain delta message reads
- `services/agent_runner/agent_host/daemon.py:build_checkpointer` — runtime checkpointer construction with `allowed_msgpack_modules`

## Checkpoint Compatibility and Serde Rules

- **Checkpoint compatibility**: nesting changes channel keys from flat (e.g., `compact_version`) to `compact` / `memory`. Old thread checkpoints lacking the new channel keys → LangGraph resume reads defaults (`from_checkpoint(MISSING)`), old flat keys are ignored (no error, no crash) — effect is that first resume resets compact/memory counters to default (self-healing, no message loss), message history and plugin channels preserved as-is.
- **Delta message reads**: `HistoryPostgresSaver` / `HistoryAsyncPostgresSaver` in
  `base/agents/history/checkpoint_postgres_walks.py` follow the requested checkpoint's exact parent chain to the nearest stored snapshot, read write keys and sizes, and transfer bodies newest-first until the latest `RemoveMessage(REMOVE_ALL_MESSAGES)` or `Overwrite`. They replay writes in upstream's order (`writes_sort_key` per checkpoint) and exclude the target's pending writes from history. Body batches are bounded to 256 KiB and 128 writes; one oversized write is fetched intact. A reset discards the older seed and prefix; a historical pre-reset request still loads them. The typed `delta_message_suffix` event uses noise-tier telemetry retention and reports actual fetched write bytes/rows separately from retained writes. The enclosing `delta_read_compat` event reports tuple/history/fold total durations and the error type; decode failures belong to the history-read phase. Read observations are explicit operation-local values; shared checkpoint serializers are not modified. Binary Postgres results preserve the existing transport format. No history is deleted or rewritten.
- **Checkpoint msgpack allowlist**: nested sub-states (`AttachState` / `AttachEntry` / `CompactState` / `MemoryState` / `ContextReset` / `CapabilitiesState`) serialize into checkpoints as pydantic-v2 ext objects carrying `(module, name)`. LangGraph's `JsonPlusSerializer` only deserializes types on its explicit allowlist; without one it warns on **every** checkpoint load ("Deserializing unregistered type agent.state.*" — once per type per process start), and a future langgraph blocks them outright. The framework's saver (`services/agent_runner/agent_host/daemon.py:build_checkpointer`) passes `allowed_msgpack_modules=checkpoint_msgpack_allowlist(plugin_state_classes)` — the nested types plus every declared plugin state class (a plugin field holding a BaseModel instance crosses the checkpointer as that class). New nested sub-states must be added to the allowlist or they degrade to raw dicts on load.

Parent: [[agent/docs/agent.meiva.okf.md]].
