# Sending and the bottom edge

A send moves nothing (user ruling 2026-11-20: keep it simple). The reader's own
message arrives in the timeline like any other row. A reader who follows the
tail keeps following it, and a reader in history keeps their place. No space
opens below the conversation for the sent message.

The centered down arrow rides to the tail and follows it from there. It is
hidden at the scrollable end, and while the question panel is open.

The ride is timed by the timeline, not by the browser's smooth scroll, whose
duration grows with the distance without bound (user ruling 2026-11-20):

- **Duration.** It grows linearly with the distance, 4px per ms, and is capped
  at one second, so the reader reaches the bottom within a second however far up
  they were. A short hop still takes 120ms, so it reads as motion.
- **Moving end.** Each frame steers toward the live natural end, so rows that
  mount or re-measure on the way move the goal, never the deadline.
- **Interruption.** Wheel, touch, keys or the scrollbar stop the ride where it
  is.

## The composer dock and pending strip

The composer occupies a fixed resting-height band below the timeline. When the
draft grows past one line (multi-line text, quotes, attachments, upload hints)
or pending user messages appear in `PendingStrip` above the composer, the dock
rises upward over the timeline's bottom edge without shrinking the timeline
viewport or pushing messages upward (user ruling 2026-11-22).

- **No push-up.** The log's bottom padding stays fixed at `1.5rem`, so growing
  the composer or pending strip never shifts `naturalEnd` or existing rows.
- **Manual scroll slack.** The dock height beyond the resting band is passed as
  `bottomOverlayHeight` (and combined with any `TimelineView` `footer` height)
  into a dedicated slack spacer below the log and into `--timeline-footer-h`.
  Covered messages stay still until the reader manually scrolls into that slack
  to lift them clear above the grown dock.
- **Collapse recovery.** When the overlay shrinks back to rest (after send or
  delivery), a reader who had scrolled into the slack clamps cleanly back to
  `naturalEnd` and resumes following the tail without a stale offset.
- **Opacity and arrow.** The dock column is opaque (`bg-background`) so scrolled
  rows stay masked underneath while desktop gutters and the right-edge
  scrollbar remain unobstructed. The centered down arrow sits `1rem` above
  `--timeline-footer-h` and stays hidden both at `naturalEnd` and inside the
  overlay slack.

## The toggle reserve

Opening or closing a work block by hand keeps its header where it is (user
ruling 2026-11-17). When the rows below are too short to keep it there, the
timeline adds reserve space below the conversation.

- **Size.** The reserve is exactly the space the header needs, and the tail
  never carries any.
- **Release.** Once the reader actively scrolls upward to or past the natural
  end, the reserve is removed in place. Movement inside the reserve keeps it, and
  layout changes alone do not spend it.
- **Saved places.** A saved place remembers whether it stood in the reserve, so
  a return or reload recreates it.

## Relationship to Other Nodes

- [Timeline sticky scroll](sticky-scroll.md) — the anchor, its one writer, and the timeline's scrollport.
