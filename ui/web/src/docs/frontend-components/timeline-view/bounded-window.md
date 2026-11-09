# Timeline bounded window

When the expanded display exceeds `display.timeline_window_activation_rows` (100 by default), `use-timeline-window` mounts the viewport plus one viewport of buffer on each side. Remote groups become two estimated-height spacers, and the children of every expanded `TurnBlock` — historical or live, because since 2026-11-09 there is no inner scrollport to own a window of its own — are windowed by that same row window above `display.timeline_window_turn_rows` (100 by default): each mounted child is wrapped in `data-virtual-row`, and `turn-before` / `turn-after` spacers preserve the height of the children that are not mounted. The count that decides activation is the number of rows an expansion really renders, so opening a long turn activates the window even when the turn itself was a single collapsed row. The window measures each mounted row's height and maps the reading position through those measurements, and the scroll position follows the last observed visible row when those estimates change. One ResizeObserver per tracking session records actual heights for mounted groups and rows (each commit observes only newly mounted nodes; the scroll listener and viewport observer are likewise installed once and read the latest groups and heights through refs). All three values come from `/api/config` with baked fallbacks. The compact transition pins a rank-qualified item through its rekey and scroll transfer; load-older pins the exact reading item through prepend. Back/forward memory also saves the visible item, rank and viewport offset, with a collapsed-turn fallback. A short timeline keeps its existing CSS containment path. The canonical item list remains in the store for parked readers; only the DOM window is released. Following readers still use the separate 250-item retention rule.

Reading-row compensation and its parked window pin yield to the sticky controller
while following. A bottom-button, send, or switch command clears the parked row
so subsequent commits cannot cancel smooth scrolling or restore an old position.

History restores wait past the mount pin for layout to hold the target; scroll/send supersedes them.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer this window bounds.
