---
type: doc
title: Capability index freshness
description: "Why the rendered `# Capabilities` index can lag a skill install, and the snapshot plus `before_llm` hook that names the difference."
tags: []
---

# Capability index freshness

The drift check is `agent/graph/prompt/capabilities.py:index_drift` plus the
`before_llm` hook in `agent/graph/hooks/capabilities.py`.

- The rendered index is a **snapshot**, built once per window; `meiva.skills.names()` under it is an **uncached filesystem scan**. Nothing reconciles them by itself, so a skill installed mid-window would be reachable by name and absent from the listing the delegation check orders the agent to match every task against — until a compaction happened to rebuild the prompt
- `init_context` records the full selected catalog membership (including folded descendants) into `state.capabilities.indexed` (see [[../../../docs/state.meiva.okf.md]]). Snapshot taken **before** the render, so a skill landing between the two is named once too many rather than dropped
- A framework-owned `before_llm` hook diffs the live membership against that record each turn and names whatever appeared in one `new_skills` system note, in the index's own line shape; the snapshot advances with the note, so one install produces one note no matter who installed it. Drift is the trigger, not a timer
- `indexed_skills()` is the single definition of membership, so narrowing needs no special case: a configured name that resolved to nothing at build time and resolves now is drift, and a skill outside a narrowed list never becomes drift
- Unresolved configured names warn on each resolution.
- `indexed: None` means no snapshot exists for this window (a checkpoint predating the field). The check then adopts the live catalog silently rather than announcing the whole catalog as new

Parent: [[agent/graph/docs/system-prompt/system-prompt.meiva.okf.md|System Prompt]].
