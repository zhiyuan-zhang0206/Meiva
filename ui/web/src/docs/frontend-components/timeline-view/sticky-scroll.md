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
echo a no-op. Wheel intent is an early release signal for fine pointers;
a scrollbar drag escapes through the controller's cumulative upward run instead;
touch devices fire no wheel. The bottom zone is pointer-dependent — touch keeps
the wide bounce-tolerant zone, mouse/trackpad a tight one so a small deliberate
scroll-up escapes — resolved once on mount, with the touch preset as the SSR
default. The viewport disables Chrome scroll anchoring (`overflow-anchor: none`):
a net-neutral reflow can move scrollTop on its own, a third hand the follow rule
cannot attribute; `overscroll-y-contain` keeps an exhausted vertical gesture out
of page-level scroll without touching scrollTop. The viewport also declares its own shrinks (`notifyContentShrunk`): after a
commit leaves the content shorter, the browser's clamp usually reaches
`handleScroll` only once a later commit added rows again, so the snapshot
carries the grown height and an upward move and the height test alone reads a
departure (#5678: a 254 px clamp from estimates replaced by measurements
released a follower for good; #5680: 69 px read as growth against a commit-only
baseline stranded one 107 px short).
A pending bookmark restore
lands before the pin, so the reader never sees the bottom first, and a
bookmark saved on the tail restores the newest message rather than a row's old
pixel: a thread that grew while the reader was away returns to its end, not to
the row they left. The save asks the follow rule itself (`isAtTail`: inside
the bottom zone, or only short of it because a chunk outran its pin) rather
than a distance, so a switch away inside a streaming frame still counts as
the tail. The scroll-to-bottom button exists only away from the bottom: presence follows
the measured distance, not the follow flag, so a height change with no scroll
event still decides it. A send anchor is the one exception — it holds the bottom
up, so the viewport reads "at bottom" while the control still has a job (resume
following); it stays shown there, because hiding it would leave the reader no way
back. At the bottom it is not rendered at all, refresh in
flight or not — a faded button left a hover target whose click did nothing (user
report 2026-10-30). While the tail read after a switch or a reconnect
(`loadingNewer`, the composed reconcile) is in flight it rests on the same ring
the pull-to-load gesture draws — one component, so the two cannot drift; it
swaps to the arrow on hover or focus, so waiting never hides that it is still a
button (user report 2026-10-19). That ring is not driven by `isFetching`: a
settled thread's background refetch would spin it for no arrival and could
overlap an older-history load for two rings at once (2026-10-20); the two loads
cannot co-occur, and the pull strip wins if they ever do. How the ring is drawn
and how it moves is
[The loading ring](loading-ring.md). Its smooth ride requests the stick instead of pinning, so the
asynchronous ride survives; growth mid-ride pins instantly because the
controller already follows. An agent switch hides the button in the same
commit, supersedes an unreachable restore target, and pins with a fresh run so a
post-command twitch cannot release following while the awaited reply streams in
(user report 2026-09-19). The
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

A send is its own arrival, not a pin: it parks the reader's own message a fixed
share down the viewport over a reserve the reply consumes, and stops following
there. [The send anchor](send-anchor.md)
owns that park, the reserve, and the controller state the reserve needs.

A viewport width change is the same problem in the other direction: every row
re-wraps, so the pixel offset that was the reader's place now points at a
different one — opening or closing the sidebar moves that column by a sixth of
the window. A reader who is not following has the bookmark's anchor re-applied
after the reflow, so they stay on the row they were reading; a reader in the
bottom zone pins as before (user report 2026-10-20).

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
- [The send anchor](send-anchor.md) — where a send parks the reader's own message and why the follow flag must ignore the bottom there.
