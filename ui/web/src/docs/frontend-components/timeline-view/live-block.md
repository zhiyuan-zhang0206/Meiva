# The expanded live block

An active turn's expanded block reports itself through
`createStickyController().notifyLiveBlock(open)`. Opening an active `Working`
block jumps directly to the bottom of its expanded content and enables
tail-following (`sticky = true`) so the reader immediately sees the latest
in-progress step and follows subsequent streamed output (user ruling 2026-11-09,
superseding the earlier fixed-top-edge rule). While the block remains open,
ordinary upward scroll or wheel gestures still release following so the reader
can inspect earlier steps inside the block. Manually expanding a settled
historical `TurnBlock` calls `notifyBlockExpanded()` instead, keeping the
viewport where the reader clicked.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the follow flag and layout passes this rule overrides while the block is open.
- [Timeline deep collapse](deep-collapse.md) — the turn grouping an expanded block comes from and its natural-height body.
