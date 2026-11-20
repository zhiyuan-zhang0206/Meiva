# Timeline history paging

## Pull gesture and request ownership

The first short viewport fills at most two older pages, stopping once content
exceeds its height. Reaching the top alone never requests history. An additional
upward wheel gesture at the top requests a page after accumulating 64 pixels
of intent. Events less than 120 ms apart form one burst. The burst that arrived
at the top and its momentum do not request a page. Remaining events from a
committed burst are consumed even if the response already prepended rows.
A touch that starts at the top commits on release after the same distance.
Multitouch, touch cancellation and reversal cancel the gesture. Keyboard users
can focus the load-earlier control.

The gesture does not add space to the timeline or displace its messages.
A reader who scrolls while waiting keeps their current reading position when
the older page is prepended: the view anchor holds the reader's row before
paint ([sticky scroll](sticky-scroll.md)). A failed or empty page leaves the list in place. A failed
request can be retried with a new gesture. Conversation changes discard
gesture state. Deliberate requests continue through backend `has_more` and
`MEIVA_TIMELINE_COMPACT_HISTORY` depth.

While following the bottom, `display.timeline_retained_items_max` (250 by default) evicts the oldest selected-thread rows and keeps older paging available. A reader parked in history keeps the loaded and visible window until returning to the bottom. The temporary compact transition buffer is kept intact through its anchor handoff, then the canonical list is trimmed.

## Which read presents the strip

The loading indicator over the timeline presents the reader's own older-page request: a
wheel or touch gesture that committed, or the keyboard paging control. The store's
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
([The loading ring](loading-ring.md)).
