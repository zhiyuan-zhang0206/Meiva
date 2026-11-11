# Restore the sidebar unread dot as one seen position per agent

## Context

Every delivery to the user is recorded in `agent_user_messages`, and the roster carried an unread
signal from the delivery-mode work
([user-delivery modes](../../engineering/notifications/2026-11-06-user-delivery-modes.md)) that no
screen showed. On 2026-11-07 #8593 and #8594 removed the unread dot from the sidebar row and the
floating unread control from the timeline, leaving an invisible per-message anchor that sent a
per-record read acknowledgement.

The project owner's ruling (2026-11-10): everything delivered to the user shows an orange dot as unread;
a pending question shows the question mark, which outranks the dot; the two never show together. A first
design (a per-record read bulk, a count, a third "awaiting a reply" state, reading only at the bottom of
the timeline) was rejected for the model below.

## Decision

1. Each agent keeps one thing: the position the user last saw, a monotonic cursor over
   `agent_user_messages.id` in a small table `agent_user_seen` (one row per agent, one human user, in
   Postgres, not Redis). There is no per-record read state and no count.
2. Unread is a boolean: a delivery with content exists past the cursor. Roster cards and snapshots carry
   `has_unread_user_messages`; `unread_user_message_count` is gone.
3. Opening the agent's conversation in a visible page counts everything delivered as seen: no scrolling to
   the bottom, no message entering the viewport. Deliveries arriving while it stays open and visible are
   seen on arrival. A hidden page sees nothing; the return to the foreground is a new look.
4. The cursor only moves forward, bounded by the agent's newest delivery id, through one idempotent keyed
   write (`GREATEST(old, N)`; `agent_updated` only when it moved). The user-messages feed returns the
   cursor as `seen_id`. The per-message resolve endpoint is removed.
5. On entry the timeline reveals the first delivery past the cursor the page held before advancing it,
   instead of landing at the bottom, by reusing the bookmark restore. With nothing unread the default
   landing stays; on a return to the foreground the viewport moves only when something new arrived.
6. The indicator has two states: the question mark when a question is pending, else the orange dot when
   unread and the agent is not the open, visible one. An open reply request (`awaiting_response_count`) no
   longer shows on the row or the hover card.
7. A turn of an external harness cut off by an interrupt or an error seals the half-written reply it had
   shown, so it is a delivery with a record like any other.

## Alternatives rejected

- A read flag per record. It is what stuck the first dot for good: a record that never renders can never
  be marked, and a failed request had no retry.
- Counting unread records. The reader needs "something new", and a count invites per-record bookkeeping.
- Reading only at the bottom of the timeline. Opening the conversation is the act of looking; the position
  tells where the new content starts.
- Marking "all" on entry. A delivery the page has not received yet would be claimed unseen; the write is
  bounded by the id the page holds, and rejected beyond the agent's newest delivery.
- A third dot state for reply requests. The notice system's own removal is a separate change.

## Consequences

- This overturns the 2026-11-07 removal of the dot; the timeline gets no unread marker back.
- Existing rows are migrated: each agent's cursor stops just before its oldest unread record, or at its
  newest when all were read. `agent_user_messages.read_at` and the partial index
  `agent_user_messages_pending` are no longer read or written and are dropped by a later migration
  (expand-contract). SDK `publications(pending_only=True)` and the restored-after-compaction reminder now
  mean "past the cursor".
- If the first unseen delivery's item is not in the loaded window the page loads up to five older pages;
  beyond that it lands on the oldest unseen item it has.
- A user with several windows shares one cursor: seeing an agent in one window clears its dot in all.

Updated by [`2026-11-11-remove-inbox-and-counts.md`](2026-11-11-remove-inbox-and-counts.md): the Inbox page,
the header aggregate and the roster counters named above are removed, not merely hidden.
