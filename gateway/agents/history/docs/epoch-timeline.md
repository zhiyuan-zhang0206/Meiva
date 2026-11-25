# Timeline across harness switches

An agent that switches harness keeps one timeline. Which harness ran when is the
`agent_harness_epochs` fact table (one row per epoch, `seq` from 0); the timeline
endpoint reads the conversation epoch by epoch, from each epoch's own source, and never
stores a copy of it ([timeline read from the record](../../../../docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md)).
A harness switch shares no conversation: the new harness cannot see anything said or
done before the switch. The restart message is the only handoff.

## Ids and cursors

- The current epoch (the highest `seq`) keeps the ids it always had: `m.b` for a native
  agent, `h<offset>.n` for an external one. Live events, streaming ids and snapshots are
  unchanged.
- An earlier epoch's items carry the prefix `e<seq>.` on their own id: `e0.12.0`,
  `e0.s1.<checkpoint>.3.0` (a compact segment of a native epoch), `e1.h123.0`.
- Each epoch after the first starts with a switch block: `system_marker`, source
  `harness_switch`, id `e<seq>.sw`, `created_at` the epoch's start. The current epoch's
  switch block is on its head page.
- An epoch whose history cannot be read is one `system_marker` with source
  `history_unavailable`, id `e<seq>.unavailable`, after its switch block.
- Order is `(epoch, part, compact segment, msg, block)` with the current epoch last,
  the switch block first, then the unavailable marker, then source items
  (`item_sort_key`). Clients sort by it and never compare `created_at` across epochs.
- A client sends the oldest id it holds as `before`, any id the endpoint emitted
  included (`e2.sw`, `e1.unavailable`).

## Pages

A page reads one source. A `before` inside an epoch pages that epoch; when the page
reaches the epoch's first item it ends with the epoch's switch block, and `has_more` is
true while an earlier epoch exists. `before=e<seq>.sw` (and `e<seq>.unavailable`) names
the head of epoch `seq`: the page is the newest window of epoch `seq - 1`. The head page
(no `before`) is the current epoch's head window, plus its switch block when the window
reaches the epoch's first item.

`msg_count` and `reading_source` describe the current epoch; a page of an earlier epoch
carries `msg_count` 0 and no `reading_source`. `current_epoch` is the current epoch's
`seq` (0 when the agent never switched). Items of an earlier epoch have no
`reading_locator`: `/timeline/window` and quotes do not serve them.

## Reading one epoch

`epoch_pages.py` plans a page and assembles it; `timeline.py` reads the sources. Both are used only when the agent has two or more epochs, they form one chain ending on the agent's harness (`usable_epochs`), and the agent row names the same harness as the last one. Any other agent is read as before, with `current_epoch` 0.

- **Native epoch**: the checkpoint is read as always, current segment and retained compact segments, through the [native slice](../../../../base/agents/epochs/docs/native-slice.md) of the epoch, so only the items of its time are windowed. A retained segment that holds nothing of the epoch is passed over inside the same request. The slice applies to the current epoch too when the agent has an earlier native epoch.
- **External epoch**: one `harness_timeline_read` to the agent's machine. The epoch's byte range bounds the window: the window starts below `source_end` (or the cursor) and stops at `source_start`. A missing bound is inferred from the next or previous epoch of the same harness. When neither bound is known and the harness has another epoch, the items are filtered to `[started_at, ended_at)` by `created_at`; this is the best effort for backfilled rows. An epoch that is not current names its session, and a record whose session differs reads as unavailable.
- **Unavailable epoch**: a machine that is offline (503) or fails the read (502), a missing or undecodable record, a session that was replaced, or a harness with no readable record. The page carries the epoch's switch block and one `history_unavailable` marker, and `has_more` continues to the epoch before it. A current epoch whose record is not written yet (no session, no file) is empty, not unavailable.

`has_more` is true while the epoch holds older items, and after its last item while an earlier epoch exists. A source that reports more but returns nothing is taken as read to its head, so a page always moves the cursor on to the switch block.
