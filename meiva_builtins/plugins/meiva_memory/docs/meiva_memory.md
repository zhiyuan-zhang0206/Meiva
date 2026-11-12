# meiva_memory — Memory Pool Plugin

## What it is

`meiva_memory` provides shared memory pool capability: passive memory recall (`MEIVA_PASSIVE_MEMORY_RECALL`), the `meiva.memory.*` SDK surface, and the standing memory context notes — the shared pool index, the per-agent index, and the ancestors' `memory/INHERIT.md` files (`inheritance/inherit.py`, whose decisions are the pure `inheritance/inherited_blocks.py`). Whether recall runs, and the knobs that shape it, are [Memory Recall — Passive Memory Recall](memory-recall.md)'s to state.

## Registered hooks

There is no write-time hook: `meiva.files` stores a note exactly as written, and the pool's rules are checked by the validator at commit and Dream time ([Memory Pool Scaffold and Validator](memory-pool-scaffold.md)).

### Passive memory recall (before_turn hook)

```python
class _PassiveMemoryRecallHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[MeivaContext], _config: RunnableConfig, /
    ) -> dict | None: ...

passive_memory_recall_before_llm = _PassiveMemoryRecallHook()
# declared in agent_runtime.py:contribute(): before_turn=(passive_memory_recall_before_llm,)
```

**Trigger and behavior**: the hook is a no-op when `settings.passive_memory_recall_enabled` is off, uses `tail_has_recallable_inbound` to trigger when the tail since the last `AIMessage` contains fresh user chat, peer-agent (`agent:`), scheduled (`schedule:`), or system inbound, skipping machine-originated `watcher:` and `shell:` wake-ups, defers (returns None) when auto-compact would also fire in the same turn, and injects the matched note references as `system_note` messages the agent sees on its next turn; the built-in `memory` sub-state's `injected_paths` holds this window's notes. The gate, the deferral reason and the injected shape: [Passive Recall Hook](memory-recall-hook.md).

The recall engine (search + rendering beyond gating) is in the core `agent/graph/recall/memory_recall.py:passive_memory_recall`;
this plugin only provides the before_turn wiring + trigger gate. It also bundles memory skills: `meiva.skills.meiva_memory`
(Two-Stage OKF: pure-Markdown notes at write time, enrichment during Dream) and
`meiva.skills.meiva_memory.consolidation` (four-phase **Meiva Dream**: orientation,
gather signal, consolidate, prune and index). `dream_gather.py` gathers workspace
candidates across hosts and tracks content hashes in
`sources/dream/workspace-memory-ledger.json`; `scope_trapped_candidates` is a P0
promotion signal. After promotion, private notes are pruned to canonical pointers.

**Host-side scaffolding and validator**: explicit `meiva memory init` brings up this cluster's memory pool checkouts and lays the plugin's `template/` down inside the authoring one (the `memory/MEMORY.md` index and the pre-commit character-cap guard); the pool's own `validate.py` runs on every commit. Idempotent, non-destructive, and converge and service startup never touch the memory repository: [Memory Pool Scaffold and Validator](memory-pool-scaffold.md).
Skill nodes see [Meiva Memory Skills](../skills/README.md).

## Provided SDK surface

This plugin **owns** the `meiva.memory` namespace — `plugin.py` assembles it and declares it as `SdkNamespace("memory", ...)`. There is no core `meiva/memory.py`; disabling the plugin removes the surface entirely.

- `meiva.memory.SHARED_PATH` — shared memory root path (`$MEIVA_HOME/vault/memory`, from the bound context's Meiva home each time it is read, so reading it outside a bound context raises), read by all agents
- `meiva.memory.search(query, k=5)` — semantic search, returns `list[MemoryMatch]` (`path`, `description`, `tags`, `score`, `chunk`), most relevant first
  (description empty string when absent; tags are empty or come from optional frontmatter tags)
- `meiva.memory.PERSONAL_PATH` — the agent's personal memory folder (`<workspace>/memory`), resolved from the SDK identity when read; maintained by that agent alone

**External harness handoff**: for Claude Code, Codex and Antigravity the plugin contributes the `meiva.memory` entry point, one boundary rule (prefer Meiva Memory over the CLI's own memory or notes file) and a `Meiva Memory` handoff section built from the handoff's `home` and `workspace`. The section names both folders by absolute path, says what belongs in shared memory and what personal memory keeps across compactions and sessions, where each `MEMORY.md` index and `INHERIT.md` live, and that the CLI's own file tools read and write them (`agent_runtime.py:_memory_handoff_section`; the section mechanism is in [the handoff document](../../../../base/lm/harness/docs/handoff.md)).

There is no writer: an agent writes notes with `meiva.files.write` (see [Memory API](memory-api.md)). `IndexerUnavailable` remains reachable but deliberately excluded from `help(meiva.memory)`.

## Key dependencies

- [Agent Hooks](../../../../agent/hooks/docs/hooks.md) — before_turn hook system
- [Standing Context — What Sits Behind the System Prompt](../../../../agent/graph/docs/context-notes/context-notes.md) — where this plugin's index + inherited notes are laid down
- [Context Window — Context Management](../../../../agent/graph/docs/context-window/context-window.md) — memory injection affects context
- [Memory-Indexer — Memory Pool Vector Index](../../../../services/docs/gateway_side/memory_indexer/memory-indexer.md) — background indexing service
- [Language Model Provider Layer](../../../../base/lm/docs/lm/lm.md) — semantic search uses LLM embeddings

## Configuration

- Host `indexer_enabled` in `default_config.py` defaults to true independently
  of `MEIVA_MEMORY_INDEX_INJECT`: disabling prompt injection leaves search available.
  Run `meiva plugins update` (also in start converge), set it false in
  `$MEIVA_HOME/configs/meiva_memory/config.json`, then use normal stop/start to disable
  indexing. Agent overlays cannot change it. Read-only roster/status evaluation
  uses defaults for an absent image without writing; invalid existing data reports
  a plugin load failure and skips the service. The image is sealed at service birth.
- Optional `lfs_enabled` (false) and `lfs_url` (empty) configure Git LFS at the
  Vault root when both enabled and given a nonblank URL; see
  [Vault and optional Git LFS](memory-pool-scaffold.md#vault-and-optional-git-lfs).
- Pool path: `$MEIVA_HOME/vault/memory`, resolved per access; no `MEIVA_MEMORY_POOL` variable.
- `settings.passive_memory_recall_enabled`: passive recall feature toggle (default in [Memory Recall — Passive Memory Recall](memory-recall.md))
- `settings.agent.memory_inherit_depth`: ancestor hops a descendant inherits `memory/INHERIT.md` from (per-agent, default 1; 0 disables). Size guardrails: `memory_inherit_max_block_chars` / `memory_inherit_max_total_chars` (0 disables one)
- Memory note format: pure Markdown (`# Title` + high-level description paragraph + body; YAML frontmatter is optional)

## Notes

- Memory notes are shared cross-agent — a note written by one agent can be searched by another
- Passive recall is "on-demand" rather than "full" — triggers on recallable inbound and skips machine-originated wake-ups
- The mutual exclusion with auto-compact is carefully designed — `messages` have a reducer that merges, but compact's REMOVE_ALL full replacement swallows same-turn appended notes, memory recall yields priority to compaction

Memory note builders read live gates and caps through their owning AgentSlices at the existing use points.
