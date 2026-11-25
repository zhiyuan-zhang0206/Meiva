# Timeline bounded window

When the expanded display exceeds `display.timeline_window_activation_rows` (100 by default), `use-timeline-window` mounts the viewport plus one viewport of buffer on each side. Remote groups become two estimated-height spacers, and the children of every expanded `TurnBlock` are windowed by the same row window above `display.timeline_window_turn_rows` (100 by default): each mounted child is wrapped in `data-virtual-row`, and `turn-before` / `turn-after` spacers preserve the height of the children that are not mounted. Activation counts the rows an expansion really renders. One ResizeObserver per tracking session records the measured heights of mounted groups and rows, and a group or row is also measured in the layout effect of the commit that mounts it, so a spacer that later replaces it already carries its real height (a row recorded only on the next frame is still an estimate when the window slides past it within the same task, and the view anchor then chases the shifted row back and forth until React stops the loop). All three values come from `/api/config` with baked fallbacks.

The window never moves the reader: spacer estimates only size the scrollbar, and the view anchor ([sticky scroll](sticky-scroll.md)) keeps the reader's row at its offset when an estimate is replaced by a measurement. A placement pins its target row so the window mounts it before the writer reads it. Below the activation threshold every row renders at its real height; there is no off-screen placeholder, because one painted frame at a placeholder height was a visible jump for every newly mounted row.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer this window bounds.
