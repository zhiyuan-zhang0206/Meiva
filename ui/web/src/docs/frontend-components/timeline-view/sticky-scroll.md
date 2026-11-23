# Timeline sticky scroll

## One anchor, one writer

The view anchor (`components/timeline/anchor`) holds the tail, an exact row and
viewport offset, or the tail bounded by an unread row. One writer
(`use-viewport-anchor`) applies it after each commit and through ResizeObserver
on the viewport and log. The same pass handles older pages, compaction, image
size, a streaming reply, and virtual spacer estimates. The viewport uses
`overflow-anchor: none` so the browser does not also anchor the content.

A reader's scroll is read back from the DOM. Within 1px of the natural end,
the reader resumes following the tail. Before reaching it, the first readable
row and its offset preserve the reader's exact scroll position. The
pointer-dependent visibility zone for the down arrow is independent of tail
following. Upward wheel and touch intent release the tail before the scroll
event so a concurrent stream cannot pull back the reader. Placements set the
anchor through the same writer, including a quote, a toggle and the down arrow.
The arrow animates the move and releases the unread boundary.

## Opening a conversation

Each agent mounts its own view. A reader who left in history returns to the
saved row and offset. A memory bookmark takes precedence over a per-tab reload
bookmark. Retained rows can open on the saved row while the authority read is in
flight. A missing bookmark row is resolved by id and rank, then source locator,
content or compact coordinate. The search can read up to 5 older pages
(250 items, 10 s). An unreachable bookmark falls back to the tail.

A reader who left at the tail keeps a watermark of the last human message or
visible reply, including a partial reply's text. On return, the fresh entry
seen cursor identifies the first unread delivery. The watermark can also
identify a new or extended reply that has yet to be published. The timeline
waits for the qualified snapshot on a tail return. The seen acknowledgement
waits until the opening rows are visible at their destination. A return
interrupted while loading leaves the old watermark and seen cursor available
for the next entry. With no new reply or unread delivery the view opens at the tail. A first visit
with unread deliveries uses the same unread placement. The existing fast path
opens a known-quiet first visit at the tail.

The boundary includes the previous human prompt when it belongs to the same
continuous question and answer: the prompt is on the same compact rank and
only reasoning, code, code output, attachments or reply visibility metadata
intervene. An earlier chat, another inbound message, a wake or a lifecycle
marker ends the search. If the watermark is missing or the scan reaches the
first loaded row through continuous work, placement uses the same bounded
older-page search as bookmark restore. Pages read to locate the delivery
count toward the five-page limit. At the bound, the best loaded reply can
still serve as the boundary. With no loaded candidate, the opening falls
back to the tail.

The writer opens as far down as the boundary permits. The scroll target is
`max(0, min(dockSafeEnd, boundaryTop - topSafeInset))`. The top clearance is
56px. The resting composer band is outside the viewport. The dock-safe end
includes the measured overlap of any footer or grown composer dock. Short
content aligns to that safe end, including when the whole conversation fits.
Long content keeps the boundary at the top and lets the reply continue below.
The writer reapplies the bound as the reply grows or layout changes. Manual
scrolling takes over from the boundary.

The rows remain hidden under the loading ring while their opening location
resolves. [Cold fill](history-paging.md) can load older pages when the initial
page does not fill the viewport. A reload from the tail keeps no session
bookmark and uses the normal opening rules.

## Expanded turn headers

An expanded Work, Context or Details `TurnBlock` has an opaque header that
sticks at the 44px lower edge of `HeaderBar` and releases at the end of the
block. The toggle stays clickable there. Closing the block positions the
collapsed header at the clicked height after the body is removed, using toggle
reserve when needed. The header sits above ordinary rows and below the
scrollbar and floating controls. Primary-message and nested card headers stay
in normal flow. A jump to a quote source inside the block adds the measured
title height to the reading offset. Ordinary scroll reads a row only once
some of it clears its expanded turn title.

The block body uses the timeline's single scrollport at its natural height.
The outer and inner virtual windows keep their spacer elements in that flow,
so the sticky header's containing block includes the unmounted rows. See
[the expanded live block](live-block.md) for the live case.

Event timestamps remain visible in message-card headers at narrow widths.
Standing context notes omit timestamps according to their item metadata.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer whose one scroll surface this node owns.
- [Timeline deep collapse](deep-collapse.md) — the turns whose natural-height bodies the live-block rule keeps in place.
- [Sending and the bottom edge](send-anchor.md) — what a send does (nothing), the down arrow, the pending strip over the bottom edge, and the toggle reserve.
