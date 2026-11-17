# The loading ring

`components/timeline/overlays.tsx` shares one ring between the reader's
older-page request, initial source qualification, and the wait for a first work item.
Each draws an arc with a 15% opacity track and an 8-unit radius in a 24-unit
box. The older-page ring overlays the top of the viewport without changing
the message layout. Both top loaders are horizontally centered in the
conversation pane (`TimelineView`'s relative wrapper), independent of the
message column width. The reader-request overlay keeps its top offset below
the floating header, using the same center anchor as the scroll-to-bottom
control.

`TimelineView.loading` means the selected source has not completed its initial
read or qualification handoff. It presents the initial-load ring even when retained
rows are available; row count does not establish source readiness. An initial
read takes precedence over a reader's older-page ring. Both suppress the inline
first-step wait, so overlapping read states present one ring. Background paging
retains its existing invisible presentation.

Once the source is ready, an active turn with no group or only a trailing human
prompt presents the inline `PendingLiveActivityWindow` ring. It keeps the work
header's row geometry and a screen-reader-only responding label, without a
visible title or disclosure chevron. A real work group keeps its expandable
`TurnBlock` content and status throughout reads. Presentation never clears a run
or applies a timeout; source failures release `loading` through their owner and
Home retains its existing retry/error UI.

A reader who asked for `prefers-reduced-motion: reduce` gets the same ring
breathing (opacity only, `animate-pulse`) instead of a frozen frame: a still ring
reads as a stuck loader, not a busy one.

The ring is mounted for one reader request and stays that same element until the
request lands: a re-render reuses the `<svg>`, and a CSS animation restarts only
when its element is created or its animation name changes, so the spin runs on
rather than starting over. The store's in-flight flag also covers cold fill and compact-history retention.
The reader request alone presents the older-page ring. See
[Timeline history paging](history-paging.md).
