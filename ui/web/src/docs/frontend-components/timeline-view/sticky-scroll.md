# Timeline sticky scroll

## Sticky bottom and scroll ownership
While following the tail, content growth pins the viewport down in the same
pre-paint tick — the ResizeObserver fires after layout and before paint — so
streamed text never paints below the fold and then scrolls into place, and
growth with no items change (throttled markdown/highlight flushes, async images)
rides the same signal. The follow flag has a single owner
(`lib/layout/sticky.ts:createStickyController`): the view wires DOM events in
(`handleScroll` / `handleWheel` / `handleLayoutChange`), performs the scrolls the
controller requests (`requestStick` / `pinToBottom`) and reports every performed
scroll back, which keeps the controller's baseline truthful and makes each pin's
echo a no-op. An upward wheel gesture releases following before the next layout pass; the
controller also tracks upward travel for scrollbar drags and handles touch
separately. The bottom zone is pointer-dependent, with a wider zone on touch
for bounce tolerance. The viewport disables Chrome scroll anchoring (`overflow-anchor: none`):
a net-neutral reflow can move scrollTop on its own, a third hand the follow rule
cannot attribute; `overscroll-y-contain` keeps an exhausted vertical gesture out
of page-level scroll without touching scrollTop. The viewport also declares its own shrinks (`notifyContentShrunk`): after a
commit leaves the content shorter, the browser's clamp usually reaches
`handleScroll` only once a later commit added rows again, so the snapshot
carries the grown height and an upward move and the height test alone reads a
departure (#5678: a 254 px clamp from estimates replaced by measurements
released a follower for good; #5680: 69 px read as growth against a commit-only
baseline stranded one 107 px short).
A pending bookmark restore lands before the pin. A follower returns to the
natural end of the conversation, including messages that arrived while away.
A reader at the end of the additional scroll space returns to their saved row
and offset. When collapsing a work block created that space, the restore
recreates as much of it as the saved offset needs. Wheel, touch, keyboard
scrolling and track interaction reveal the timeline scrollbar. Position restores
and layout updates keep an idle scrollbar hidden.
The down arrow sits at the bottom center of the conversation and uses the same
icon while loading or generating. It takes the reader to the last human message
or the tail according to the [reading position](send-anchor.md). A smooth ride
to the tail can be interrupted by reader navigation. The
bookmark owns the viewport only until it settles or the reader moves: a settled
restore and any movement the controller classifies both release the hold, so a
reader who scrolls after returning is no longer read as "none" (that silence
used to switch following off for good and skipped the position save on the next
agent switch — user report 2026-10-19). A bookmark past the end of a page
that has stopped growing is unreachable: it lands on the end and hands the
viewport back instead of holding the reader at the top forever. The composer's pending strip lives outside the scroll
area, so the echo arrives as ordinary growth and a following reader stays on the
tail either way. Only a following reader releases the oldest rows (the 250-row
retention rule in the paging doc).

A viewport width change is the same problem in the other direction: every row
re-wraps, so the pixel offset that was the reader's place now points at a
different one — opening or closing the sidebar moves that column by a sixth of
the window. A reader who is not following has the bookmark's anchor re-applied
after the reflow, so they stay on the row they were reading; a reader in the
bottom zone pins as before (user report 2026-10-20).

A `reveal` request (`TimelineView` prop: an item id and a token) moves the viewport the way
a reader's own bookmark would, to the item at the upper part of the viewport (the bar height
plus 12 px): it fills the pending restore, takes the hold, and lets the existing
apply/settle/window-materialization path land it, so no second scroll path exists. Each
new token is a new request, an item that is not loaded changes nothing, and the reader's
own scrolling releases it like any bookmark. The home page uses it to show the first
delivery past the reader's seen position on entry. When the reader returns to saved
history, that position takes priority over the automatic reveal.

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
