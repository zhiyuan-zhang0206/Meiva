# The expanded live block

Manually opening an active `Working` block or a settled `TurnBlock` keeps its
header at the clicked viewport offset and leaves the reader anchored. Opening
also cancels an ongoing ride to the bottom. Subsequent streaming growth and
layout measurements preserve that reading position. The explicit down arrow
and a new send retain their existing navigation behavior. An automatically
expanded block follows the reader's current scroll mode.

This follows the user ruling of 2026-11-17, superseding the 2026-11-09 decision
to jump to the bottom and follow when manually opening active work. Manual
collapse preserves the header offset while the shortened timeline can reach
it. At the natural end it resumes tail following. This decision applies once
to the collapse placement, so a later resize of a historical row does not
change the reader's scroll mode.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — scroll ownership and layout passes after a manual expansion.
- [Timeline deep collapse](deep-collapse.md) — the turn grouping an expanded block comes from and its natural-height body.
