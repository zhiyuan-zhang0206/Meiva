# The expanded live block

An active turn's expanded block reports itself through
`createStickyController().notifyLiveBlock(open)`. Opening one stops following —
the block's header is the reader's place from then on, not the tail — and while it
stays open a layout pass never pins, so the block's top edge holds and its content
extends downward with no bound. The flag suppresses the pin only: the reader's own
return to the bottom still re-follows, and the first layout pass after the block
closes or collapses pins as usual. Without it, every streamed chunk pinned the
tail and carried the header the reader was reading off the top of the screen
(user ruling 2026-11-09).

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the follow flag and layout passes this rule overrides while the block is open.
- [Timeline deep collapse](deep-collapse.md) — the turn grouping an expanded block comes from and its natural-height body.
