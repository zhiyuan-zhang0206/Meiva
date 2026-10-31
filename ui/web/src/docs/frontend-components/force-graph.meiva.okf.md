---
type: doc
title: Force Graph
description: The shared force-directed SVG canvas (`components/fleet/force-graph.tsx`, `force-graph-geometry.ts`) behind Fleet GraphView and TaskGraph — hover card placement, safe-zone bridge, edge legibility, and large-graph settle timing.
tags:
- frontend
- fleet
---

# Force Graph

`components/fleet/force-graph.tsx` and `components/fleet/force-graph-geometry.ts`
implement the shared force-directed canvas rendered by both `GraphView` (circle
agent nodes) and `TaskGraph` (square task nodes). Its d3 zoom/pan behavior,
transform state, reset, and box-fit math come from `lib/fleet/use-svg-zoom-pan.ts`,
also used by the memory graph; each canvas retains its own layout timing and
focus-neighbor bounds.

Label sizing/wrapping and its direct contracts use `fleet/presentation/labels.ts`;
`force-graph-geometry.ts` retains focus/hover hooks and graph model declarations.
Task canvas/chrome/contracts share the records and legend in `presentation/task-palette.ts`.
`layoutOptions` remains owned by the actual `ForceGraph` renderer.

## Hover card geometry and pointer transitions

The hover card's geometry and pointer transitions are pure functions of the boxes
involved in `force-graph-geometry.ts`:

- `placeHoverCard` — places the card 6 px beside the node top-aligned, flips to
  its left edge near the right canvas boundary, slides upward within the canvas
  column (or caps `maxHeight` to the full column height) when tall, centres above
  or below on a narrow canvas, and lifts above the legend when columns overlap.
- `hoverCardBridge` — sizes an invisible safe-zone strip spanning the gap toward
  the anchor node on the outer overflow-visible tooltip shell, outside the inner
  scroll container so `overflow-y: auto` never clips hit-testing across the gap.
- `hoverCardLeave` — holds immediately when the pointer crosses onto the partner
  node or card, and otherwise waits out `HOVER_CARD_HIDE_GRACE_MS` symmetrically
  in both directions so node-to-card and card-to-node travel never flickers.

Task titles, descriptions and results lay out in full inside `TaskHoverCard`
(`task-graph-chrome.tsx`); tall cards scroll via the inner scroll container
while `ResizeObserver` watches the uncapped content wrapper for async growth.
Selection rings (`isRinged`) carry `pointerEvents="none"` and node bounds read
`[data-node-shape]` so dashed ring gaps do not trigger spurious leave events.

## Edge hierarchy and dark-mode legibility

Every edge uses `text-muted-foreground` (`stroke="currentColor"`), distinguished
by opacity and width alone. Incident edges highlight at `0.96` opacity; unselected
lineage edges render at `0.86`; dimmed lineage edges (when another node or Inbox
agent is selected) stay at `0.48` opacity rather than near-zero so the task tree
skeleton remains readable on dark canvases. Stroke widths scale gently by
`Math.sqrt(viewSpan / 900)` above a 900-unit `viewBox` span (capped at `2x`) so
multi-branch graphs do not shrink into sub-pixel hairlines.

## Navigation and layout timing on a large graph

Navigation is a value the surfaces are handed, not a module they read:
`app/fleet/page.tsx` reads the router, wraps `push` as `navigate` and passes it
down through `FleetView`, `LeftGraphPanel` and the mobile layout to `GraphView`,
`TaskGraph` and its Kanban cards. A double-clicked node opens
`/?agent_id=<owner>` through that prop, and a suite hands in a spy instead of
replacing `next/navigation`.

A graph of 200 nodes or more (`layoutOptions` in `force-graph.tsx`) settles in
manual, time-budgeted slices instead of the live d3 timer: the simulation steps
under a per-frame budget and commits positions explicitly, twice early and then
every 32 slices. Below that threshold the live settle stays, because a small
graph animating into place is the point of it.

The reason is the render, not the physics. Each live tick commits a new position
map through React state, and each commit repaints the whole SVG: the preview's
1,949-node graph spent **4.4 s of main-thread time in its first six seconds** at
4x CPU throttle, and the profile attributed 4.6 s to native layout/style/paint
with only tens of milliseconds in JavaScript. `useForceLayout`'s manual path was
built for the memory graph earlier (task #4008); this is the same treatment for
the two Fleet canvases.

### How long the entrance runs

Below the threshold the settle is the visible entrance, so `alphaDecay` is what
the reader feels. The shared default is **0.08** (82 ticks, ~1.4 s at 60 fps,
79 px of average node travel on the 40-node agent graph) rather than d3's 0.0228
(295 ticks, 4.9 s, 108 px). The user asked for the landing to stay and the
movement to shrink (2026-10-21). Only the default moved: a browser holding a
stored `display.*_force_params` keeps its own value, and the slider range stays
0.001-0.2.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md|Fleet View]] — the supervision surface whose `GraphView` and `TaskGraph` tabs mount this canvas.
- [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md|Frontend Components]] — the component catalog this node hangs from.
