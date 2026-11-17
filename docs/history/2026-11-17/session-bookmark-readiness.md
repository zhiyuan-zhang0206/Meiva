# Session bookmark restoration waits for the readable window

Returning to a conversation could consume its bookmark against an empty loading
layout. Repeated ResizeObserver deliveries at the same short height were treated
as evidence that the saved position was unreachable. A second boundary existed
between a successful query render and the passive effect applying its snapshot
to the timeline store.

The restore now waits for loading to finish, and loading includes the handoff to
the readable store. The store owns an explicit snapshot-ready flag, set by a retained seed or an
applied snapshot, including an authoritative empty result. A mutable hook ref
alone left empty responses waiting indefinitely.

The existing reachable-end fallback remains available after readiness. Following,
reserve ownership, and seen/unread cursors retain their existing contracts.
Refresh persistence and seeking a historical source window are a separate scope.

Validation uses the actual HomePageView → useTimeline → TimelineView consumer,
with controlled DOM geometry and resize delivery. A cold return fails at 0px on
the baseline and restores the departed 700px bookmark after the fix; the retained
window return is a positive control. No production data was used.
