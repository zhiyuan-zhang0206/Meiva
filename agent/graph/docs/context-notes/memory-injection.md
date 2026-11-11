# Memory Injection — The Three Memory Notes

## Responsibility

`meiva_memory` owns every memory context note; disabling the plugin removes the
stores, the SDK surface, the notes, and the write-discipline section together
(`notes.py` builds the two index notes —
[Memory index notes — the two stores' standing context notes](../../../../meiva_builtins/plugins/meiva_memory/docs/memory-index-notes.md) — `inheritance/inherit.py` the
inherited one —
[Memory inheritance — INHERIT.md read off the birth chain](../../../../meiva_builtins/plugins/meiva_memory/docs/memory-inheritance.md) — and `agent_runtime.py`
registers all three, the `memory_discipline_section` prompt section, and the recall
hook; `plugin.py` declares the `meiva.memory` SDK surface). All are laid down by `init_context` behind
the SystemMessage; see [[agent/graph/docs/context-notes/context-notes.md|Standing
Context]] for the registry itself.

## The notes

- **Shared MEMORY.md** (rank 20; memory pool root `meiva.memory.PATH`, pointer
  index, visible to all agents) — `settings.agent.memory_index_inject_enabled`.
  Not `on_fork` (cluster-wide; grafting duplicated it — issue #1320).
- **Per-agent memory** (rank 40; workspace `memory/` dir: `memory/MEMORY.md`
  index, entries as sibling files read on demand) —
  `settings.agent.memory_per_agent_inject_enabled`, `on_fork`. Empty index
  created if missing. **Only the index is injected**; past
  `memory_per_agent_index_max_lines` (default 200, 0=off) a maintenance
  reminder is appended — no truncation.
- **Inherited memory** (rank 45; `inheritance/inherit.py`): the `memory/INHERIT.md`
  files of an agent's ancestors (pure Markdown in their workspaces), read at every
  window establishment from the first `memory_inherit_depth` hops (default 1,
  0..10, 0=off) up the immutable birth chain (`agents_meta.born_spawner`;
  on a fork the chain is the fork source's). Chain read: one light gateway call
  (`GET /api/agents/{id}/born-chain`, no tie graph), cached per process — the
  chain is immutable; a failed read degrades to no note and retries at the next
  establishment. Content is read off THIS machine's workspaces, so only
  ancestors that ran here contribute — a remote ancestor is skipped and named
  in a footer (same-machine read, v1). Size guardrails
  (`memory_inherit_max_block_chars` / `memory_inherit_max_total_chars`, 0=off)
  truncate oversized declarations with a visible marker plus a warning — never
  silently. Content carries no timestamps, so unchanged state is byte-stable.
  `on_fork`: the inherited note carries the SOURCE's chain, so `_handle_fork`
  grafts the new agent's own after the fork marker (the inherited copy stays in
  the source's history — see [[agent/graph/docs/context-notes/context-notes.md|Standing
  Context]]).

`inherited_blocks.extract_inherit_body` normalizes CRLF, strips an optional
leading YAML frontmatter block with `_frontmatter_body`, and trims whitespace.

## Write discipline

`agent_runtime.py:memory_discipline_section` supplies fixed system-prompt text
for three layers: the shared pool at `meiva.memory.PATH`, personal state in
`memory/MEMORY.md` and `memory/<slug>.md`, and descendant context in
`memory/INHERIT.md`. Shared notes hold reusable rules, broadly useful facts and
user rulings; personal notes may retain verbose working state.

Two-Stage OKF keeps writing simple: agents write pure Markdown (`# Title`, a
high-level description paragraph, then the body), without write-time frontmatter
or `type/*` tags. Provenance uses inline body Markdown file links. Meiva Dream
adds canonical OKF description and lifecycle fields during consolidation.

The discipline weighs self-verifying sources (code, cluster state, filesystem),
asserted sources (maintained project instructions and docs), and remembered
claims. Check current evidence and timestamps when they disagree; repo facts
carry a path, command or SHA verification point. Search before writing, update
canonical notes in place, and correct stale claims in the same turn they are
noticed, before continuing the task.
