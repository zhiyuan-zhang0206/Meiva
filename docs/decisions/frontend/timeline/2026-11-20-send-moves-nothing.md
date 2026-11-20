# A send moves nothing; the pending strip floats in the bottom gap

## Context

From 2026-11-16 a send parked the reader's message at the reading offset. A
reserve below the conversation held it there and let the reader scroll past the
natural end, and the down arrow stopped at the latest human message first.

On 2026-11-20 the user asked for both behaviours to go: "keep it simple". In the
same session they reported two more problems with queued messages:

- the pending strip pushes the gap above the composer upward instead of
  occupying it;
- the UI jitters on every queued send.

Two mechanisms caused this:

- **The strip was in normal flow above the composer.** Its appearance shrank
  the timeline's viewport by its full height, about 53px, and the tail moved the
  conversation up by as much.
- **The send park stayed armed until its row arrived.** A queued message arrives
  only when the agent claims it, so the view jumped to park it at that later
  moment.

## Decision

- **A send is not a placement.** The anchor alone decides: a follower keeps
  following the tail, and a reader in history keeps their row. This is the
  2026-10-19 rule ([a send moves only a following reader](2026-10-19-send-never-moves-the-viewport.md)),
  now with no code of its own. The store's send request, the park, the send
  reserve, and its saved and session flags are deleted.
- **The arrow rides to the tail.** It no longer stops at the latest human message.
- **The header-toggle reserve stays.** It serves the 2026-11-17 ruling that a
  toggled header stays where it was clicked, which is a different behaviour.
- **The pending strip is the timeline's footer.** It floats over the bottom edge
  and publishes its height as one CSS variable. The log's bottom padding is
  `max(1.5rem, footer - 1rem)`: the strip may use the padding plus the 1rem
  already blank below the last row. One queued message fits, and only a taller
  strip extends the padding.

## Alternatives rejected

- **Keep the strip in flow and shrink it.** A 53px strip still moves the
  conversation by its height. Shrinking it only makes the jump smaller.
- **Compensate the strip's height in the writer.** That is a second, cause-specific
  writer, which the single-anchor rule
  ([2026-11-20-timeline-single-anchor.md](2026-11-20-timeline-single-anchor.md))
  exists to prevent. A footer that changes padding is an ordinary layout change
  the writer already handles.
- **Let the strip cover the last row's hover chrome band unconditionally.** That
  is tolerable only because messages queue while the agent works, and then the
  last row is the working turn, which has no chrome there.

## Consequences

- One queued message appears and leaves without moving the conversation. Each
  further one moves a following reader by its own row height, and a reader in
  history not at all.
- Saved places written before this change may carry `keepSendReserve`. It is
  ignored.
