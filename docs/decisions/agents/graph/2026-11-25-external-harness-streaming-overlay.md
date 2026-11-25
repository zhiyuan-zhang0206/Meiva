# An external agent's reply streams as a temporary overlay that the record replaces

Lifts decision 6 ("streaming is out for now") of
[the CLI's own transcript is the source](2026-11-12-transcript-is-the-timeline-source.md), on the terms that decision
reserved: a separate, non-durable layer, never the source of the record. Decisions 1 to 5 and
[the timeline is read from the record](2026-11-13-timeline-read-from-the-record.md) stand.

## Context

An external agent's reply appeared whole, when the CLI wrote its row, while a built-in agent's appeared word by word.
The user asked on 2026-11-25 for streaming for all three CLIs. The adapters already produce the text as it arrives
(`TextDelta`, `ReasoningDelta`); the timeline sink dropped it, because the record has no token-level content and a
streamed message that the record never has was the inconsistency that sank the earlier designs.

## Decision

1. **The stream is shown, never stored.** The sink turns an adapter's deltas into the same `chat_*` / `reasoning_*`
   events a built-in agent emits, under temporary ids `live.<turn id>.<block>`. Nothing is written to the database or to a
   checkpoint, and nothing reaches the projection, a delivery or usage. A delta stays droppable: losing one changes what
   is shown for a moment, never what is committed.
2. **A block is the adapter's id for it.** `TextDelta` / `ReasoningDelta` carry an optional `block`: Claude Code
   `<message.id>:<block index>`, Codex the item id, Antigravity none (the sink numbers its blocks within the turn).
   The record's rows carry the same id (`message.id` with `apiBlockIndex`; the item id), so the parser keeps it on the entries.
3. **The record replaces the overlay by one rule and two fallbacks.** The snapshot that commits a block lists the live ids
   it supersedes in `TimelineSnapshot.retires`, in the same message, so the page never shows both or neither. The block is
   found by (a) the same `block` id; (b) when either side has no id, the committed text starting with the streamed text,
   both stripped; (c) the end of the turn, an interruption or a fault, which retires everything left after one read of the
   record. The read comes first: Claude Code writes the partial text of an interrupted message as a row within
   milliseconds of the stream's end, so retiring first would remove the text and bring it back.
4. **A retired block does not return.** Pending deltas are sent before a retirement, deltas of a retired block, of a block
   whose id the record already committed, and of a finished turn are dropped.
5. **Not covered.** A page opened in the middle of a turn shows the committed rows and the stream from then on, not what was
   streamed before. Tool-call arguments are not streamed. Codex reasoning is not streamed (its summary arrives whole just
   before the item is complete). A turn ended by a process that dies leaves its blocks on the open pages until they reload.

## Alternatives rejected

- **Store the stream, or the CLI's rows, to reconcile exactly.** The decisions of 2026-11-12 and 2026-11-13 are the reason.
- **A new event type for retirement.** `retires` on the snapshot makes the replacement atomic; a separate event would let a page
  show a block twice or not at all between two messages.
- **Predict the committed id.** A row's id is its byte offset in the file, unknown until the row is written.
