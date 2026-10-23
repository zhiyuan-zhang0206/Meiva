---
type: doc
title: meiva_memory — Memory Pool Plugin
description: '`meiva_memory` provides shared memory pool capability: passive memory recall (auto semantic search on new user message arrival and inject relevant note references), the `meiva.memory.*` SDK surface, and the write check on notes written with `meiva.files`.'
tags:
- extensions
- plugin
- agent-extension
---

# meiva_memory — Memory Pool Plugin

## What it is

`meiva_memory` provides shared memory pool capability: passive memory recall (`MEIVA_PASSIVE_MEMORY_RECALL`), the `meiva.memory.*` SDK surface, the write check, and the standing memory context notes — the shared pool index, the per-agent index, and the chain-inherited `inheritable` blocks (`inherit.py`, whose decisions are the pure `inherited_blocks.py`). Whether recall runs, and the knobs that shape it, are [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]]'s to state.

## Registered hooks

### Memory write check (after_exec hook)

`_MemoryWriteCheckHook` checks the notes the `files.write` / `append` / `edit` wraps recorded and adds at most one `memory_write_check` reminder; it never blocks, rewrites or raises. Details: [[meiva_builtins/plugins/meiva_memory/docs/memory-write-check.meiva.okf.md]].

### Passive memory recall (before_llm hook)

```python
class _PassiveMemoryRecallHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[MeivaContext], _config: RunnableConfig, /
    ) -> dict | None: ...

passive_memory_recall_before_llm = _PassiveMemoryRecallHook()
# declared in contribute(): before_llm=(passive_memory_recall_before_llm,)
```

**Trigger and behavior**: the hook is a no-op when `settings.passive_memory_recall_enabled` is off, triggers when any inbound since the last AIMessage is not from a peer agent, defers (returns None) when auto-compact would also fire in the same turn, and injects the matched note references as `system_note` messages the agent sees on its next turn; the built-in `memory` sub-state's `injected_paths` holds this window's notes. The gate, the deferral reason and the injected shape: [[meiva_builtins/plugins/meiva_memory/docs/memory-recall-hook.meiva.okf.md|Passive Recall Hook]].

The recall engine (search + rendering beyond gating) is in the core `agent/graph/recall/memory_recall.py:passive_memory_recall`;
this plugin only provides the before_llm wiring + trigger gate. It also bundles memory skills: `meiva.skills.meiva_memory`
(Memory Steward maintenance manual — daily merge, health check, note consolidation, query service) +
`meiva.skills.meiva_memory.consolidation` sub-skill (`meiva memory` CLI daily merge — commit / push / rebuild index).

**Host-side scaffolding and validator**: explicit `meiva memory init` brings up this cluster's memory pool checkouts and lays the plugin's `template/` down inside the authoring one (the `MEMORY.md` index and the pre-commit character-cap guard); the pool's own `validate.py` runs on every commit. Idempotent, non-destructive, and converge and service startup never touch the memory repository: [[meiva_builtins/plugins/meiva_memory/docs/memory-pool-scaffold.meiva.okf.md|Memory Pool Scaffold and Validator]].
Skill nodes see [[meiva_builtins/plugins/meiva_memory/skills/docs/skills.meiva.okf.md|Meiva Memory Skills]].

## Provided SDK surface

This plugin **owns** the `meiva.memory` namespace — `plugin.py` assembles it and declares it as `SdkNamespace("memory", ...)`. There is no core `meiva/memory.py`; disabling the plugin removes the surface entirely.

- `meiva.memory.PATH` — memory pool root path (`$MEIVA_HOME/memory`, computed by `meiva_home()` each time it is read), shared by all agents
- `meiva.memory.search(query, k=5)` — semantic search, returns `list[MemoryMatch]` (`path`, `description`, `tags`, `score`, `chunk`), most relevant first
  (description empty string when absent; tags include the note's `type/<x>` tag so a caller can weigh a hit by its kind)
- `meiva.memory.PERSONAL_PATH` — the agent's own memory folder (`<workspace>/memory`), resolved from the SDK identity when read

There is no writer: an agent writes notes with `meiva.files.write` (see the write check above). `IndexerUnavailable` remains reachable but deliberately excluded from `help(meiva.memory)`.

## Key dependencies

- [[agent/hooks/docs/hooks.meiva.okf.md]] — before_llm hook system
- [[agent/graph/docs/context-notes/context-notes.meiva.okf.md]] — where this plugin's index + inherited notes are laid down
- [[context-window.meiva.okf.md]] — memory injection affects context
- [[memory-indexer.meiva.okf.md]] — background indexing service
- [[base/lm/docs/lm/lm.meiva.okf.md]] — semantic search uses LLM embeddings

## Configuration

- Host `indexer_enabled` in `default_config.py` defaults to true independently
  of `MEIVA_MEMORY_INDEX_INJECT`: disabling prompt injection leaves search available.
  Run `meiva plugins update` (also in start converge), set it false in
  `$MEIVA_HOME/configs/meiva_memory/config.json`, then use normal stop/start to disable
  indexing. Agent overlays cannot change it. Read-only roster/status evaluation
  uses defaults for an absent image without writing; invalid existing data reports
  a plugin load failure and skips the service. The image is sealed at service birth.
- Pool path: `$MEIVA_HOME/memory`, resolved per access; no `MEIVA_MEMORY_POOL` variable.
- `memory_write_check_deadline_seconds` (5.0) / `memory_write_dedup_threshold` (0.80): the write check's budget and duplicate threshold
- `settings.passive_memory_recall_enabled`: passive recall feature toggle (default in [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]])
- `settings.agent.memory_inherit_depth`: ancestor hops a descendant inherits `inheritable` blocks from (per-agent, default 1; 0 disables). Size guardrails: `memory_inherit_max_block_chars` / `memory_inherit_max_total_chars` (0 disables one)
- Memory note format: YAML frontmatter (`type: Memory`, `meiva_agent`, etc.) + markdown body

## Notes

- Memory notes are shared cross-agent — a note written by one agent can be searched by another
- Passive recall is "on-demand" rather than "full" — only triggers when tail has non-agent inbound, avoiding search on every turn
- The mutual exclusion with auto-compact is carefully designed — `messages` have a reducer that merges, but compact's REMOVE_ALL full replacement swallows same-turn appended notes, memory recall yields priority to compaction

Memory note builders read live gates and caps through their owning AgentSlices at the existing use points.
