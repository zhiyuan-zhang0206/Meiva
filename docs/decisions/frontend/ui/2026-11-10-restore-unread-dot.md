# Restore the sidebar unread dot, one indicator per row

## Context

Every delivery to the user is recorded in `agent_user_messages` with a `read_at`, and
the roster has carried `unread_user_message_count` since the delivery-mode work
([user-delivery modes](../../engineering/notifications/2026-11-06-user-delivery-modes.md)).
On 2026-11-07 #8593 and #8594 removed the unread dot from the sidebar row and the floating
unread control from the timeline, leaving only an invisible per-message anchor that sent a
read acknowledgement when its row scrolled fully into view. The count stayed on the wire
and nothing on screen showed it, so a reader supervising many agents could not tell which
of them had said something since they last looked.

Restoring the dot on top of the anchor alone would have been wrong: a delivery was marked
read only if its own row rendered and fully entered the viewport, so a folded row, a record
older than the loaded history window, a PREFIX/NAIVE record that matches no timeline row, a
failed request (never retried), or `intersectionRatio === 1` never reached under fractional
zoom left the dot on for good.

## Decision

- A row shows at most one indicator. A pending question shows the question mark and
  outranks everything. Otherwise an open reply request or an unread delivery shows the same
  Sunset Orange solid dot. The two never show side by side; they differ only in the accessible
  name ("Awaiting a reply" / "Unread update"). The hover card uses the same judgement and
  colors (it used notice-priority colors, which put an Ask in blue).
- Read is a rule about the conversation, not about a rendered row. When an agent's
  conversation is open, the document is visible and the timeline rests at the latest
  position (the signal behind the scroll-to-bottom control) for a short dwell, the page sends
  one idempotent request that marks that agent's unread deliveries with id up to the highest
  record id it has fetched. Newer records the page has not received stay unread.
- The per-message resolve endpoint, the per-row anchor and the manual read control are
  removed; the bulk endpoint is the only way to mark deliveries read.
- A failed request is retried with growing waits while the conditions hold, then waits for the
  next trigger (visible again, at the bottom again, a newer record, a feed refetch).
- The conversation being read shows no unread dot: a delivery arriving under the reader's
  eyes does not light the row and go out. A reply request or question still shows.
- A turn of an external harness cut off by an interrupt or an error seals the half-written
  reply it had already shown, so that reply is a delivery with a record like any other.

The timeline gets no unread marker back: the floating controls #8593 removed stay removed.

## Alternatives rejected

- Keep only the per-message anchor and fix its threshold and retry. It cannot clear records
  that never render, which is the permanent stuck dot.
- Mark everything read when the conversation opens. It marks records the reader has not seen
  (a long history scrolled to a bookmark; deliveries that arrive while the page is hidden).
- Mark "all" in the bulk request. A record delivered after the page last fetched would be
  marked read unseen; the request is bounded by the id the page has actually received.
- A second, differently colored dot for unread. The reader asked for a single orange signal
  with the question mark outranking it.

## Consequences

- This overturns the 2026-11-07 removal of the dot; the count it shows was always there.
- Reading is judged by position, not by individual messages: an old unread record the reader
  scrolled past while a newer one sat below the viewport stays unread until the reader
  reaches the latest position.
- A reader who leaves the timeline scrolled up on a quiet agent keeps its dot lit until they
  return to the bottom. That is the intended meaning of unread.
- The roster count reads the partial index `agent_user_messages_pending`; no schema change.
