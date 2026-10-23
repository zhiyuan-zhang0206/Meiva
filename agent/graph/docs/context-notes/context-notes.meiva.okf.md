---
type: doc
title: Standing Context — What Sits Behind the System Prompt
description: The standing head of an agent's context window — the SystemMessage plus the ordered context notes behind it, who owns them, and how memory notes reach them. Laid down by the `init_context` node whenever a window is established.
tags: []
---

# Standing Context — What Sits Behind the System Prompt

## What It Is

Standing context notes belong to the Meiva harness: its `execute_code` system
prompt and ordered context notes. `init_context` establishes them on first wake
and after compaction. External harnesses own their provider CLI prompts and do
not build Meiva standing notes; instead, `init_context` lays down their rendered
handoff document as `SystemMessage` at `messages[0]` with `harness_cursor = 0` so
the timeline and the first submitted turn share one source of truth.

An empty message channel is the establishment signal, without a separate mode.
A compaction clears it and parks the summary in `context_reset.tail`; new chats
remain pending inbounds for claim. Meiva appends that tail after the head. An
external harness places the tail behind `SystemMessage(handoff.render())` and
resets its submission cursor to zero, so both reach its next turn. An intact
history is a pass-through; a stale parked tail is discarded with a warning,
without splicing into it.

Notes tell the agent what changed and what to do, in its own terms.

## Core Mechanisms

### Standing Context Notes (`context_notes.py`, laid down by the `init_context` node)
- One ordered list, one owner: `init_context` runs before `claim` and lays down the head (SystemMessage + notes) whenever `messages` is empty. A compaction empties the window, parks the tail in `state.context_reset`, and routes back here, so cold start and post-compact are one code path.
- Framework notes (`FRAMEWORK_NOTES`): agent id (`on_fork`; states id, label, machine, workspace path), exec timeout, cluster timezone, preloaded skills (`on_fork`, issue #1320). A plugin declares its own in `contribute()`; `MeivaContext.extensions` supplies them ([[docs/plugins/declared-contributions.meiva.okf.md]]). `on_fork` = grafted onto a fork's inherited history (`fork_notes`).
- Rank is a prompt-cache decision as much as a reading-order one. Ranks 10-19 are the stable band: cluster-identical bytes (one cached prefix shared by every agent on the API key) that change only on a config edit already requiring an agent restart. Rank 20 — the shared memory index — is re-read off disk at every establishment, so anything behind it re-caches whenever any agent writes memory. The cluster-timezone note sits at rank 15 for exactly this reason.
- **Cluster timezone** (rank 15): states the IANA name of the clock the turn's context carries plus the numeric offset in force at that clock's instant (`agent/graph/prompt/note_text.py:timezone_text(name, now)`). It exists so the timestamps do not have to carry one — the setting is cluster-pinned, so the suffix was a constant repeated on every stamp, and an ambiguous constant at that (`%Z` is `PDT` in June and `PST` in December for one unchanged setting; `CST` names two zones). Not `on_fork`: a fork stays in the same cluster, so the inherited declaration is still true.
- **Agent identity** (rank 30): states id, label, host machine, and workspace. The wording is a function of the facts it states (`agent/graph/prompt/note_text.py:agent_id_text`, a `strict` module); `context_notes.py` reads them: the label row through `ctx.sql`, the machine through `ctx.machine`, the workspace folder and its shared document through `workspace_facts(path, home)`. Missing rows or empty labels omit the label clause; SQL and programming errors propagate to the context-building caller. An unset machine name omits its clause. The `# Workspace` prompt section is id-free for fork safety: this `on_fork` note holds the concrete path, and a fork regrafts its own. Each builder receives `MeivaContext`: identity uses `agent_id(ctx)` with lease validation, labels use `ctx.sql`, and settings use `ctx.agent`; there is no host-wide SDK binding.
- **Forks**: a fork keeps the inherited head untouched; `_handle_fork` appends the `lifecycle_fork` marker, `fork_notes()` and any tail-graft skills at the tail, so the source's cached prefix is reused. The shared memory index stays NOT `on_fork`. See [[agent/graph/docs/context-notes/fork-context.meiva.okf.md|Fork Context]].

### Memory Injection (`plugins/meiva_memory/notes.py` + `inherit.py`, declared by the plugin)
- Both stores belong to `meiva_memory`; disabling it removes stores, SDK surface, notes, and discipline section together. The three notes — shared pool index, per-agent index, and the `inheritable` blocks read from the ancestor chain — their ranks, switches, fork behavior, and guardrails are documented in [[agent/graph/docs/context-notes/memory-injection.meiva.okf.md|Memory Injection]].

### Passive Recall (`memory_recall.py`, decisions in `memory_note.py`)
- Content-driven recall on new inbound wake-up turns, scored by a decision model: the notes that clear the score threshold are injected behind the head with the passage that matched, and a failed search is a logged no-op, never fatal. The `passive_memory_recall_enabled` slice field gates the stage. The trigger gate, scoring, dedup and failure rules are in [[agent/graph/docs/context-notes/passive-recall.meiva.okf.md|Passive Recall]]; the recall switch and defaults are stated in [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]].

### Context Structure
- System prompt (fixed, included in every LLM call)
- Standing context notes (exec timeout / cluster timezone / agent id / preloaded skills / the two memory indexes / the chain-inherited memory blocks) laid down behind the system prompt whenever the window is established
- Message history (user messages + agent replies + tool call results)
- Current turn input

## Key Dependencies

- [[context-window.meiva.okf.md]] — compaction is what makes the head need re-establishing
- [[system-prompt.meiva.okf.md]] — the head's first message, and where the memory discipline lives as a section
- [[meiva_builtins/plugins/meiva_memory/docs/memory-recall.meiva.okf.md]] — passive recall in depth

## Entry Points
- `agent/graph/_init_context.py` — the node that lays down the head
- `agent/graph/prompt/context_notes.py` — `context_notes` / `fork_notes`, framework notes; `note_text.py` — their wording
- `meiva_builtins/plugins/meiva_memory/notes.py` — the two memory index notes; `inherit.py` — the inherited-memory note (ancestor chain, block parsing, guardrails)
- `agent/graph/recall/memory_recall.py` — passive recall
