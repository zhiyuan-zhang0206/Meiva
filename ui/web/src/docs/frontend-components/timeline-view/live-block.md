# The expanded live block

Manually opening an active `Working` block jumps to the bottom of its expanded
content and starts following new output. An automatically expanded block follows
the reader's current scroll mode. In either case, the reader can scroll up to
inspect earlier steps. Manually expanding a settled `TurnBlock` keeps the
viewport at the clicked position. Manual collapse preserves the header offset
and temporarily adds scroll space if the shortened content cannot reach it.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the follow flag and layout passes this rule overrides while the block is open.
- [Timeline deep collapse](deep-collapse.md) — the turn grouping an expanded block comes from and its natural-height body.
