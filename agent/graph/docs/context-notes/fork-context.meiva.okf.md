---
type: doc
title: Fork Context — What a Fork Keeps and What Is Grafted at the Tail
description: How `_handle_fork` builds a fork's context window — the inherited head is left untouched, the fork marker and `fork_notes()` are appended at the tail, and tail-graft skills ride the fork inbound after them — so the source's cached prefix is reused.
tags:
- context
- fork
---

# Fork Context — What a Fork Keeps and What Is Grafted at the Tail

## Responsibility

A fork copies its source's history; `_handle_fork` then decides what changes in
the window. Everything that changes lands at the tail, so the prefix the source
already cached stays valid. The registry of standing notes, ranks and the
`on_fork` flag are [[agent/graph/docs/context-notes/context-notes.meiva.okf.md|Standing Context]]'s to
state.

## Behavior

- **Fork keeps the inherited head (2026-10-17)**: `_handle_fork` leaves the copied messages untouched and appends the `lifecycle_fork` marker plus `fork_notes()` at the tail. The shared memory index stays NOT `on_fork` (cluster-wide; grafting it duplicated the index — the timezone rule). An earlier head rebuild that stripped the source-identity notes broke the prefix cache the fork rides ([decision](../../../../docs/decisions/agents/context/2026-10-17-fork-keeps-inherited-prefix.md)).
- **Fork tail-graft skills (task #2694)**: the fork inbound's payload carries `tail_skills` — inject-list skills the fork's config added beyond the source's, minus what the fork's expand list already grafts. `_handle_fork` resolves them (the shared `skill_selection.resolve_prompt_skills`; names it cannot resolve are reported with a warning and skipped) and appends one note with their full SKILL.md bodies AFTER the fork marker and `fork_notes()` — the tail, never the cached prefix. Tagged `preloaded_skills` like the framework's preloaded-skills note. Expand-list additions need no extra mechanism: the fork's own preloaded-skills note already covers them.

Parent: [[agent/graph/docs/context-notes/context-notes.meiva.okf.md|Standing Context]].
