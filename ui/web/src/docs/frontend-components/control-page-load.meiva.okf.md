---
type: doc
title: Control and Insights first paint
description: Why the vertical anchor pages paint their headings before their section bodies, the bounded deadline that schedules the bodies, and what that costs.
tags: [frontend, performance, control, insights]
---

# Control and Insights first paint

Both pages share one section wrapper, `app/control/_section.tsx`. A section
renders its heading and anchor immediately and mounts its body on the frame
after the first paint, bounded by `DEFERRED_MOUNT_DEADLINE_MS`
(`lib/layout/use-client-ready.ts`, 200 ms).

The reason is the route transition, not the data. Control mounts nine section
bodies — Config alone carries nineteen groups of fields — and the App Router does
not commit the new URL until the incoming tree has rendered. Measured on the
deployed build with real data, 4x CPU throttling, and the API proxied on the same
host: the click took 1362 ms to change the URL and added **13,878 DOM nodes** in
that one transition (script 580 ms, layout 258 ms, style 337 ms). Insights and
Fleet, which mount far less, committed in 342 ms and 469 ms with ~300 nodes each.
Data was never the bottleneck — Control's seven API reads started only after the
commit.

What the split must not change:

- **Anchors stay on the `<section>`**, so `#config` and the nav rail resolve
  before the body exists.
- **Deep links still land.** `useSettledAnchorScroll` re-applies the scroll while
  content above the target is still growing; that hook's logic is unchanged, and
  the deferred bodies are one more growth source it already tolerates.
- **Queries still start on mount of the body**, one frame after paint. They are
  still "started from the page's first paint" in the sense that matters: before
  a reader can scroll to the section.

The deadline exists because `requestIdleCallback` alone is not a promise: a page
that never reaches idle would hold every body back indefinitely. At the deadline
the body mounts anyway.
