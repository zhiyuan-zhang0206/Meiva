# Timeline View

`components/timeline/` renders the BackendTimelineItem list (chat / code / output / reasoning / system marker) and is the only item-list surface of a thread. Directory: `index`, `segments` (compact grouping), `use-compact-transition-anchor` (reading position), `use-timeline-window` (bounded DOM), `card`, `item`, `buttons`, `markers`, `timestamp`, `reasoning-clock`, `runs` (turn grouping), `run-block`.

## Streaming rows

Memoization, clock ownership and announcements:
[Streaming rows](streaming-rows.md).

## Returning to an agent

The page keys each mounted timeline by agent and scopes its in-memory bookmark
by page-entry plus agent. A bookmark has one of two kinds: inside the bottom
zone the reader was watching the tail, so it says "follow the bottom" and the
return lands on the newest message however much arrived meanwhile; above it the
reader keeps the visible message coordinate and viewport offset, and returning
materializes the prefix before restoring that anchor. Work/detail expansion
choices are retained under the same identity. An item bookmark
survives resize, tail repair and trim until the reader scrolls or explicitly
requests the bottom; a tail bookmark follows the end as the thread grows.
Sending still jumps to the latest message; a first visit starts at the bottom.

The existing bottom button keeps one fixed footprint and its downward arrow.
A thin rotating ring runs along its edge while the latest read is queued/running,
including the composed re-attach read. The button stays enabled: clicking during
refresh jumps to the current bottom and resumes following incoming messages.
Finishing removes only the ring, without moving messages. The position is saved before the pull hook's cleanup removes its native reserve. Older paging retains its separate top pull circle. Cold views
without retained rows keep the existing initial-load UI.

## Scroll ownership and one scrollport

While following the tail, content growth pins the viewport down in the same pre-paint tick, and the follow flag has a single owner (`lib/layout/sticky.ts:createStickyController`) fed by DOM events, with every performed scroll reported back so a pin's echo is a no-op. Work and detail headers stay in normal document flow without `position: sticky` (user ruling 2026-11-01), and the timeline has exactly one scrollport: an expanded `TurnBlock` renders its body at its natural height instead of wrapping it in a capped `ScrollArea`, so a finished block is as tall as the work really was and the reader scrolls the timeline itself. The ongoing block keeps its top edge instead — growth extends it downward and never pins — which the controller owns through `notifyLiveBlock` (user ruling 2026-11-09). The pull reserve, the wheel and touch zones, and the scroll-anchoring opt-out live in [Timeline sticky scroll](sticky-scroll.md).

## Continuous segments

Compact boundaries add no separator, label, gap, or virtual height to the message stream, and original messages remain readable in order across each boundary; during a transition the view renders buffered old groups above the new rank-0 window under provisional ranks until a single store update re-keys them. Prepend materialization, document-space anchor compensation and landing detection — including the no-double-count rule (#1272) and QA #3031 — are in [Timeline continuous segments](continuous-segments.md).

## Cross-compact paging

Position changes alone never request history: inertia, scrollbar moves, layout
clamps and prepend echoes all stop at the same boundary.


Earlier pages use a released native pull with a circular progress indicator and
stable prepend compensation. Gesture ownership, the prepared reserve and selected
row retention are described in [Timeline history paging](history-paging.md).

## Bounded mounted window

Long histories mount only the viewport plus one viewport of buffer on each side: [Timeline bounded window](bounded-window.md).

After a compact, `useCompactHistoryRetention` automatically walks all available older pages above the new summary serially (`unlimited` retention, user ruling 2026-11-01). The store edge and retention hook live in [Selected Timeline State](../../frontend-state/timeline-cache.md).

## Deep collapse & Unified TurnBlock

Secondary items group into unboxed inline `TurnBlock`s whose classifier, expansion defaults, turn timer, and natural-height body live in [Timeline deep collapse](deep-collapse.md).

## Column geometry

The scroll surface is full-bleed — the scrollbar sits at the pane edge and the
gutters scroll — while the item column stays capped (`display.timeline_width_px`,
default 800px) and centered inside it. The column clears the floating HeaderBar
with 52px (44px bar + 8px clearance, derived from `BAR_HEIGHT_PX`; user ruling
2026-08-06) so a bar-height change cannot slide the first row underneath it. The
composer stack lives in normal flow below the surface (user ruling 2026-08-06),
separated from the last message row by `pb-6` + `pt-0.5` (26px) and from the
bottom viewport edge by its own band's `pb-6`, and shares the same column
cap with one `undefined`-means-full-width convention on narrow viewports.


## Relationship to Other Nodes

- [Frontend Components](../frontend-components.md) — the catalog this node was split out of; the sidebar, composer, and inspector live there.
- [Per-Thread Timeline Cache](../../frontend-state/timeline-cache.md) — the store that feeds this view, including the compact-replace edge and the retention re-attach.
- [Card chrome](card-chrome.md) — the hover band in the gap under a block (timestamp and Copy) and its pointer gating.
- [System markers](markers.md) — system-marker dispatch and the fail-loud unrecognized alarm.
- [The loading ring](loading-ring.md) — the one ring every loading state draws, the pull scale that rides `scale` instead of `transform`, and the reduced-motion state.
- [The expanded live block](live-block.md) — why an expanded ongoing block holds the top edge instead of the tail.
