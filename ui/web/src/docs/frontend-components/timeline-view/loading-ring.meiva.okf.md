---
type: doc
title: The loading ring
description: "The one ring the timeline draws for every loading state — its geometry, the pull scale that rides the `scale` property, and the two motion states (turning, or breathing when the reader asked for reduced motion)."
tags:
- frontend
---

# The loading ring

`components/timeline/overlays.tsx` draws one ring for every loading state of the
conversation: the pull-to-load strip, the scroll-to-bottom button while the
newest page is read, and the cold load of a thread with nothing on screen yet.
One geometry, so the three cannot drift — an arc of the same circle, the track at
15% opacity, r=8 in a 24-unit box, at `size-5`. An arc fraction of 1 is a whole
circle; the pull strip's grows with the pull, the button's is fixed. The button
draws the ring rather than a circle of its own because a circle drawn to the
button's edges, r=16.5 in a 36-unit box, read as a dial rather than a loader
(user report 2026-10-20).

The ring turns while the load is in flight. The pull strip's ring scales with the
revealed pull space, so the first pixels of a pull show a small whole circle
instead of a fixed circle sliced into a bar by the strip's clipping (same
report). That scale rides the `scale` property, never `transform`: an inline
transform makes the spin keyframes interpolate as matrices, and `rotate(360deg)`
decomposes to `0deg`, so the ring paints one frame forever while
`getAnimations()` still reports it running (user report 2026-10-29: "it doesn't
spin any more, it's a static animation").

A reader who asked for `prefers-reduced-motion: reduce` gets the same ring
breathing (opacity only, `animate-pulse`) instead of a frozen frame: a still ring
reads as a stuck loader, not a busy one.
