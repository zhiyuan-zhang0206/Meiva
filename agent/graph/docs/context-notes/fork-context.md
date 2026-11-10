# Fork Context — What a Fork Keeps and What Is Grafted at the Tail

## Responsibility

A fork copies its source's history; `_handle_fork` then decides what changes in
the window. Everything that changes lands at the tail, so the prefix the source
already cached stays valid. The registry of standing notes, ranks and the
`on_fork` flag are [Standing Context](context-notes.md)'s to
state.

## Behavior

- **Fork keeps the inherited head (2026-10-17)**: `_handle_fork` leaves the copied messages untouched and appends the `lifecycle_fork` marker plus `fork_notes()` at the tail. The shared memory index stays NOT `on_fork` (cluster-wide; grafting it duplicated the index — the timezone rule). An earlier head rebuild that stripped the source-identity notes broke the prefix cache the fork rides ([decision](../../../../docs/decisions/agents/context/2026-10-17-fork-keeps-inherited-prefix.md)).
- **Fork tail-graft skills (task #2694)**: the fork inbound's payload carries `tail_skills` — inject-list skills the fork's config added beyond the source's, minus what the fork's expand list already grafts. `_handle_fork` resolves them (the shared `skill_selection.resolve_prompt_skills`; names it cannot resolve are reported with a warning and skipped) and appends one note with their full SKILL.md bodies AFTER the fork marker and `fork_notes()` — the tail, never the cached prefix. Tagged `preloaded_skills` like the framework's preloaded-skills note. Expand-list additions need no extra mechanism: the fork's own preloaded-skills note already covers them.

- **External harness forks (2026-11-11)**: a Claude Code or Codex fork inherits its source's CLI session instead of checkpoints ([how](../../../../base/lm/harness/docs/fork.md)); the same marker, notes and tail skills are its first input, and the marker also names the child's own workspace, because the cloned session was started in the source's.

Parent: [Standing Context](context-notes.md).
