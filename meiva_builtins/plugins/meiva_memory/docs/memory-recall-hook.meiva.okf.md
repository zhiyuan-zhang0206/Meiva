---
type: doc
title: Passive Recall Hook — Trigger Gate and Injection Behavior
description: The before_llm hook meiva_memory registers for passive recall - its feature gate, the inbound gate that decides which turns trigger a search, why it defers to auto-compact in the same turn, and what a hit looks like once injected as a system note.
tags:
- plugin
- memory
- extensions
---

# Passive Recall Hook — Trigger Gate and Injection Behavior

## Responsibility

`_PassiveMemoryRecallHook` is the plugin's `before_llm` wiring for passive recall:
it decides whether this turn searches and where the result lands. The search,
scoring and rendering are the core engine's
(`agent/graph/recall/memory_recall.py:passive_memory_recall`); the recall
mechanism as a whole is
[[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]]'s to state, and the
plugin is [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md|meiva_memory]]'s.

## Trigger conditions

- **Feature gate**: `settings.passive_memory_recall_enabled` (env `MEIVA_PASSIVE_MEMORY_RECALL`) — the entire hook is a no-op when it is off; its default and the cluster opt-out are in [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]]
- Positive gate `tail_has_user_inbound` (`agent/messages/__init__.py:223-232`): **since last AIMessage** any inbound whose source **does not start with `agent:`** triggers — user / system / schedule / watcher / shell sources **all pass**, **only peer agent (`agent:` prefix) is excluded**; no non-empty text check, not "most recent one", but scans tail inbound
- If auto-compact hook would also trigger in the same turn, **defers** (returns None) — `messages` have add_messages reducer that merges, without failing loud, but auto-compact's REMOVE_ALL full replacement is order-sensitive and would swallow the same-turn appended note

## Behavior

- Performs semantic search against recent conversation, injects matched note references as system messages (`system_note`), seen by agent in next turn. Each injected note presents path, score, frontmatter description and the passage that matched — `meiva.memory.search`'s fields minus tags (the injected note is a pointer, not a tag list). Description is empty when absent, never synthesized from title/body
- The built-in `memory` sub-state's `injected_paths` (reducer `memory_state_merge`) holds this window's notes; the hook resets it when `compact.version` passes `window`

## Entry Points

- `meiva_builtins/plugins/meiva_memory/agent_runtime.py` — the plugin's agent-side hook wiring
- `agent/graph/recall/memory_recall.py` — the recall engine the hook calls

Parent: [[meiva_builtins/plugins/meiva_memory/docs/meiva_memory.meiva.okf.md|meiva_memory]].
