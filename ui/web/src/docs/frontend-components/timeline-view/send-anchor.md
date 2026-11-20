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

## The pending strip

Messages waiting for the agent float over the timeline's bottom edge, just above
the composer's rule. The strip is the timeline's footer
(`TimelineView` `footer`). Its height is published as `--timeline-footer-h` on
the timeline wrapper.

- **Fitting into the gap.** The strip sits in the blank space between the last
  row and the composer: the log's 1.5rem bottom padding, plus the 1rem below the
  last row (its group's padding and endRef's gap). One queued message fits that
  space, so it appears and leaves without moving the conversation.
- **Taller strips.** A taller strip extends the padding by the difference
  (`max(1.5rem, footer - 1rem)`). The tail then follows the new end, and a
  reader in history keeps their row.
- **Hover chrome.** While messages wait, the last row is the working turn, which
  carries no hover chrome in that space.
- **Opacity.** The strip is opaque, so rows scrolled beneath it stay hidden.
- **The arrow.** The arrow sits above the strip.

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
