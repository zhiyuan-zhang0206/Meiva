# A send moves only a following reader

## Context

Since [2026-07-17-sticky-single-force-scroll-trigger.md](2026-07-17-sticky-single-force-scroll-trigger.md),
`scrollToBottomRequest` had two bumpers: an agent switch and an explicit send,
and both pinned unconditionally. The send pin assumed that the message you just
typed must be visible. It fights the reader instead: with history pulled up — or
merely scrolled up while an agent streams — pressing send yanked the viewport
away from what was being read, and the message itself was never down there (it
waits in the pending strip above the composer). User report 2026-10-19,
clarified twice as the main page message list.

## Decision

A send pins only a reader who is already following the tail. In the timeline's
pin effect: a thread change always pins (mount included); a counter bump pins
only when the sticky controller is following and no restore is steering the
viewport. A reader parked mid-history is untouched.

Nothing else changes: the counter still belongs to the store, `page.tsx` still
bumps it on a successful send, and the same `useLayoutEffect` consumes it.
`restoreRequestRef` went away with the unconditional branch it served: with a
send only reaching the pin path for a follower, "a send supersedes an unreachable
restore target" no longer exists — a pending restore is simply honoured.

## Alternatives rejected

- **No signal at all (a thread change is the only pin).** Simplest to state, and
  it shipped first — but it made the viewport's only post-send commit disappear,
  and this codebase's virtualized window needs that commit: after a compact
  transition re-keys every group, rows below the viewport are spacers at their
  estimated height, and the `e2e` history-restore test caught the window sitting
  on those estimates (every row 80px) where it previously re-measured. The
  conditional pin keeps the commit and the behaviour the user asked for.
- **Bump from the composer only when `atBottom`.** The at-bottom measurement
  belongs to the timeline (its local state and pointer-dependent thresholds);
  routing it to `page.tsx` makes the page a second owner of scroll policy.
- **Reveal just the sent message (a smaller forced move).** Still a forced move
  away from the reader's position.

## Consequences

- `history-scroll.test.tsx` pins the rule directly: a send leaves a parked
  reader at their saved position (content heights 600 and 900), while a
  selection still supersedes an unreachable target.
- `tests/e2e/state/test_compact_transition.py` had been relying on the old
  unconditional pin: its `placeReply` helper assumed a fully materialized tail,
  which only held because the app left the viewer at the bottom after a send. It
  now materializes the tail first, which is what its assertion ("this parked
  position survives the compact") actually needs.
- The scroll-to-bottom button is no longer hidden by a send; the reader is
  still away from the bottom, so leaving it visible is correct.
