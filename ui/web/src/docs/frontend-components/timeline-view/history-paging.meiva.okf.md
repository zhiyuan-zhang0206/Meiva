---
type: doc
title: Timeline history paging
description: Native pull gestures, directional history loading, stable prepend anchors and retained selected rows.
tags:
- frontend
---

# Timeline history paging

## Pull gesture and request ownership

The first short viewport fills at most two older pages, stopping once content
exceeds its height. Reaching the top alone never requests history. A fresh pull
starting there reveals an 80px native scroll reserve prepared before browser
hit-testing. A view without available earlier history keeps its natural height
and scrolling; the reserve is prepared when history becomes available. Once
prepared, it remains fixed through requests and the final landing. Idle scrolling
stops at the bottom of that reserve, so it adds no
visible gap; the first physical wheel can scroll it immediately. The message list follows that
native scroll downward while a clipped circular progress spinner emerges below
HeaderBar. There are no visible pull/release instructions. A stationary gesture
keeps its position indefinitely; no inactivity timer starts a request.

A native `scrollend` commits a wheel pull at 64px. Chrome's trackpad phase keeps
that event pending while fingers remain down, including a slow lift without a
fling. `WheelEvent.momentum` can commit sooner at the first inertial event; the
consumed momentum tail stays blocked through and after landing. The gesture
that merely arrived from inside the list must end before a new pull can arm.
Wheel gesture loading requires native `scrollend`; keyboard loading remains
available when it is absent. Touch uses `touchend` and cancels on multitouch or
`touchcancel`. Reversal below the boundary, zoom and horizontal intent cancel.

The reserve and circle keep their exact pulled offset through request and
prepend compensation. The reserve height remains constant after landing,
avoiding a second reflow or scroll correction. The newly loaded prefix fills the
space above the unchanged reading anchor. Canceled, failed and empty pulls
return to the idle boundary with native smooth scrolling (instant with reduced
motion). A successful page also closes the exposed space when it inserts below
an unchanged standing-context block rather than above the visible reading anchor.
Conversation changes discard the gesture; there is no release timer. Deliberate pulls can continue
through backend `has_more` and `MEIVA_TIMELINE_COMPACT_HISTORY` depth.


While following the bottom, `display.timeline_retained_items_max` (250 by default) evicts the oldest selected-thread rows and keeps older paging available. A reader parked in history keeps the loaded and visible window until returning to the bottom. The temporary compact transition buffer is kept intact through its anchor handoff, then the canonical list is trimmed.

## Which read presents the strip

The strip above the messages presents the reader's own older-page request: a
pull gesture that committed, or the keyboard paging control. The store's
in-flight flag is not a display contract — it is the concurrency guard that every
backward read holds, and two of those reads page history nobody pulled for: the
cold first-viewport fill (at most two pages on a short thread) and
compact-history retention after a compact replaces the window. Keying the strip
on that flag lit it for both, and because every load mounted a fresh ring the
spin restarted instead of turning (user report 2026-10-29). Each request
therefore records its origin where it begins (`OlderLoadOrigin` in
`lib/timeline/timeline-plan.ts`), and only a `reader` origin is presented. One
request keeps one ring element from its start to its landing, so a parent
re-render, however frequent, never rebuilds it
([[ui/web/src/docs/frontend-components/timeline-view/loading-ring.meiva.okf.md|The loading ring]]).
