# Timeline sticky scroll

## One anchor, one writer

The reader's place is one value per conversation, the view anchor (`components/timeline/anchor`):
the tail, or a row (an item, or a whole turn block) whose top edge sits a given number of pixels
below the viewport top. One writer (`use-viewport-anchor`) applies it after every commit and on a
ResizeObserver over the viewport and the content: the anchor row goes back to its offset, the tail
goes to the natural end. Every layout change is therefore the same case and needs no code of its
own: older pages prepended, a compact re-keying rows, a width change re-wrapping them, an image or a
late height, a virtual-window spacer estimate. Chrome's own scroll anchoring stays off
(`overflow-anchor: none`) so there is no second hand.

Only the reader moves the anchor. A scroll the writer did not cause (wheel, touch, keys, scrollbar,
find-in-page) is read back from the DOM: the tail inside the bottom zone of the natural end
(pointer-dependent, wider on touch for bounce tolerance), otherwise the first row below the header
bar. An upward wheel notch leaves the tail before its scroll event, so a chunk landing first cannot
pin the reader back. While a finger is down the writer stands still. Placements only set the anchor:
returning, reloading, the first-unseen reveal, a quote, a send, a header toggle and the down arrow
(the one animated move, interrupted by any reader input).

## Opening a conversation

Each agent mounts its own view. Its opening place is the saved place (this page entry's memory,
else the tab's reload bookmark), else the first delivery past the reader's seen position, else the
tail (`anchor/placement.ts`). Rows render hidden under the one loading ring until that place
resolves in the loaded rows, so the first painted frame is the destination:

- Retained rows that hold the saved row open on it at once, while the authority read is still in
  flight. The read then merges under the anchor, and a surviving row does not move.
- A first visit waits for its rows and for the reveal decision. When the roster said the agent had
  no unread deliveries, the decision is the tail without waiting for the feed.
- A saved row that is not loaded (a reload into older history, or rows replaced while away) is
  looked up by id, then by its source `reading_locator`, then by its compact coordinate, and
  searched for in at most 5 background older pages (250 items, 10 s). An unreachable row opens the
  tail and drops its reload bookmark.

Leaving saves the row on screen and its offset, including when leaving the tail (user ruling
2026-10-17): a return continues reading there while newer output stays below. A reader who
reaches the bottom follows again. A first page shorter than the viewport stays hidden while the
cold fill reads its older pages ([history paging](history-paging.md)), so history never lands
above rows already on screen.

Across a full tab reload (`feature.reloadReadingRestore`, default `true`) the place is a per-agent
`Browser.session` bookmark; a reload from the tail keeps none and opens on the latest rows.

A `reveal` (the first delivery past the seen position) is a first-visit opening place only; a saved
place always wins over it, including when the reader returns to the bottom.

Wheel, touch, keyboard scrolling and track interaction reveal the timeline scrollbar. Placements
and layout updates keep an idle scrollbar hidden.

## Non-sticky headers and natural-height TurnBlock bodies
Human-message, agent-reply, `TurnBlock`, and nested detail-card headers all stay
in normal document flow without `position: sticky` (user ruling 2026-11-01,
replacing frosted sticky work and detail headers). An expanded `TurnBlock` — the
historical one and the active `Working` one alike — has no scrollport and no
height cap of its own: its indented body flows at its natural height inside the
timeline, so a finished block is as tall as the work really was and the reader
scrolls the timeline itself (user ruling 2026-11-09, revoking the 50%/80%
view caps and the nested wheel chaining that served them). The one
scrollport is the message list; nothing inside it can swallow a gesture, so a
scroll over an expanded block is a scroll of the timeline.

The expanded live block's edge rule has its own node:
[The expanded live block](live-block.md).

Event timestamps remain visible in message-card headers at narrow widths;
headers and timestamp text wrap when needed. Standing context notes continue
to omit timestamps according to their timeline item metadata.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer whose one scroll surface this node owns.
- [Timeline deep collapse](deep-collapse.md) — the turns whose natural-height bodies the live-block rule keeps in place.
- [The send anchor](send-anchor.md) — how sends, the down arrow, and the reading reserve interact.
