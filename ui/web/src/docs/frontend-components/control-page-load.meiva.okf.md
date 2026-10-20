---
type: doc
title: Control and Insights first paint
description: Why the vertical anchor pages paint their headings before their section bodies, and how the mount queue spreads the bodies and the config form over idle slices.
tags: [frontend, performance, control, insights]
---

# Control and Insights first paint

Both pages share one section wrapper, `app/control/_section.tsx`. A section
renders its heading and anchor immediately and mounts its body after the first
paint, one slice at a time — see the mount queue below
(`lib/layout/use-client-ready.ts`).

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

## The mount queue

Deferring past the first paint is not enough on its own. Every waiting subtree
then lands in the *same* idle callback, which is one commit — and one style and
layout pass — over all of them. Measured on the deployed build at 8x CPU
throttle after the first split: 1208 ms of main-thread blocking right after the
click, with 7874 nodes, and the config form alone was 6629 of them across 380
rows. On the reviewing machine that is a page that opens and then freezes.

So waiting subtrees queue, and the queue releases **one per idle callback**
(`DEFERRED_MOUNT_SLICE_TIMEOUT_MS`, 50 ms ceiling per release, so a page that
never idles still fills in). A section body is one entry; each config display
group inside the Config body is another, so the 380-row form arrives in
seventeen small commits instead of one large one. `DeferredMount`
(`components/app/deferred-mount.tsx`) is the same primitive for a subtree that
is not a Control section.

A URL that carries a hash opts out: `useDeferredMount` starts already mounted
when the page was opened as a deep link, so the sections above the target grow on
the same schedule they always did and `useSettledAnchorScroll` settles the jump
exactly as before. Measured: `/control#config`, `/control#schedules` and
`/insights#alerts` all land with the section at the top of the scroll container on
the deferred build.
