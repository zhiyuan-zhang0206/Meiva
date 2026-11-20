# The loading ring

`components/timeline/overlays.tsx` shares one ring between the reader's
older-page request and initial source qualification.
Each draws an arc with a 15% opacity track and an 8-unit radius in a 24-unit
box. Both top loaders — the older-page overlay and the initial-qualification
overlay — sit inside the same non-interactive floating disc surface as the
scroll-to-bottom control (`size-9 rounded-full bg-background border
border-border shadow-md pointer-events-none`), keeping a solid bordered boundary
instead of floating bare over retained message text. Both overlay the top of the
viewport without changing the message layout, horizontally centered below the
floating header (`left-1/2 -translate-x-1/2 top-14 z-20`) in `TimelineView`'s
relative wrapper, independent of the message column width.

`TimelineView.loading` means the selected source has not completed its initial
read or qualification handoff. It presents the initial-load ring even when retained
rows are available; row count does not establish source readiness. The same ring
shows while a conversation's rows are hidden until their opening place resolves
([sticky scroll](sticky-scroll.md)): retained rows that hold the saved place show at
once under the ring, rows that do not stay hidden behind it. An initial
read takes precedence over a reader's older-page ring. Both suppress the inline
first-step `PendingLiveActivityWindow` row (`!loading` in `TimelineGroupList`,
where `TimelineView` passes `loading || readerLoadingOlder`), so source and
reader loads present only the top ring without a competing bottom `Working…` row.
Background paging retains its existing invisible presentation.

Once the source is ready and no reader older-page load is active, an active turn
with no group or only a trailing human prompt presents the inline
`PendingLiveActivityWindow` (`Working…` with the `Layers` icon, matching
`TurnBlock`'s collapsed header geometry without a disclosure chevron). A real
work group keeps its expandable `TurnBlock` content and status throughout reads.
Presentation never clears a run or applies a timeout; source failures release
`loading` through their owner and Home retains its existing retry/error UI.

A reader who asked for `prefers-reduced-motion: reduce` gets the same ring
breathing (opacity only, `animate-pulse`) instead of a frozen frame: a still ring
reads as a stuck loader, not a busy one.

The ring is mounted for one reader request and stays that same element until the
request lands: a re-render reuses the `<svg>`, and a CSS animation restarts only
when its element is created or its animation name changes, so the spin runs on
rather than starting over. The store's in-flight flag also covers cold fill and compact-history retention.
The reader request alone presents the older-page ring. See
[Timeline history paging](history-paging.md).
