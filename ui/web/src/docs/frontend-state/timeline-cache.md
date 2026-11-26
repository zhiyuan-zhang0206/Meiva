# Selected Timeline State

`/api/system/all?agents=<active>` carries only the selected agent's detailed
events plus system signals. The timeline store holds one selected view.
`switchThread` atomically replaces its identity, items, streaming flags, compact
state, pagination state, and token fields; inactive agents have no live buckets
or compact-marker subscriptions.

The selected conversation's three models (timeline / token-usage / pending)
are retained per agent for the 30-minute switch window (`lib/switch-budget.ts`):
a switch back seeds from the retained window and paints immediately, and the
mount/key-change path fires no read of its own. One composed re-attach
reconcile (`agent-reconcile.ts`) refreshes all three: a stream open during an
existing read leaves a trailing composed read (including the initial no-cache
request), a second gap during the composed read leaves one more, and the
composed write never overwrites a newer read. Leaving the view aborts the
in-flight request through its AbortSignal and disposes queued reconcile work,
so query cancellation reaches `fetch`. The coordinator exposes its queued/running
activity directly through an external-store subscription. `useTimeline.isFetching`
includes that activity, including join/trailing work, and clears on abort or
selection loss even when a transport ignores cancellation. Older-page requests have selection-scoped controllers:
a late response after A-to-B-to-A cannot append to the new A view, and cancellation
does not produce an error toast or clear the new view's loading state.

Retained rows provide layout immediately. Each selection still waits for its own
accepted authority read before `sourceReady` releases bookmark restoration.
The head query carries a selection/compact-epoch receipt; the composed coordinator
publishes an accepted-read revision even when structural sharing preserves an
unchanged query object. Aborted or superseded reads publish no receipt. The
selected store's `reloadSnapshot` returns whether it actually applied the read:
a pre-compact response dropped during `resetPending` cannot qualify the entry.
Leaving and returning creates a new selection owner, so a late A1 read cannot
qualify B or A2. Initial loading includes this handoff for cached and cold entry;
an authority error settles the loading ring while restoration remains pending
until a successful retry. Once qualified, ordinary background reads keep their
fetching state without restarting the initial ring.

This readiness records a completed request and store application. Snapshot merge
still retains committed rows absent from a response, including an empty one;
readiness establishes no durable session membership or cache-eviction guarantee.

A compact replacement preserves the upstream retention edge: `compactReplaceSeq`
and `compactReplaceAgent` trigger reattachment through the same cancellable
history-page read, walking every available older page serially (`unlimited`
retention, user ruling 2026-11-01). A completed page with `has_more=false` ends
the walk; an unready window before the post-compact tail read settles does not.
Once the canonical window reaches `display.timeline_retained_items_max` (or a
following reader trims it and sets `retentionCappedSeq`), that compact's
background walk settles and retires so neither following nor parked readers
fetch unbounded historical segments in a loop.
Both the SSE reset-window snapshot and a reconnect GET that crosses an unseen
compact capture the previously displayed rows in `compactBuffer` while canonical
`items` takes the new current window. The buffer owns a thread/epoch, each row's
future historical rank, and its oldest real item ID per rank.

The SSE reduction and compact-envelope detection live in
`lib/timeline-store-events.ts`; `lib/timeline-store.ts` owns the resulting store
updates, buffer release, and display notifications. Coverage
matches canonical checkpoint-prefixed pages by rank, message/block coordinate,
and kind. The old current segment's coordinates shift down by one because the
historical checkpoint reader omits its system prompt; older historical segments
keep their existing coordinates. Partial rows, prompt/summary, and re-attached
head notes do not require a match.
Canonical historical rows stay masked while the buffer is displayed. A covering
page releases the buffer in its store update; terminal paging releases any
uncovered rows at settlement. Switch clears the buffer; hide and reconnect
invalidate a page owner's epoch while preserving it; a newer compact captures
the current display at a new epoch. Late pages cannot enter a newer epoch.

History remains fully accessible through scroll-up paging. Leaving a selection
freezes its displayed rows, compact buffer and paging boundary under the
`timeline-reading-window` query key for the same 30-minute retention window.
Returning consumes that frozen window into the selected store; leaving again
starts a fresh retention interval. The authoritative tail query remains separate
and a fresh composed reconcile merges into the retained rows. Inactive windows
own no SSE subscriptions, DOM, timers or live reducer buckets. Durable records
are untouched.
While the reader follows the bottom, `TimelineView` asks the store to trim the
oldest canonical rows to `display.timeline_retained_items_max` (250 by default)
while preserving rank-0 standing context (`0.0` prompt, contiguous head
`system_marker` notes, and rank-0 `inbound_compact_summary` — matching backend
`_missing_standing_context`) within that cap so mid-turn snapshots do not
misread re-attached summaries as an unseen compact reset and scroll-up keeps its
oldest non-standing cursor, sets `hasMoreOlder` so evicted rows can be reloaded,
and resets the next older page to its base size. The compact transition buffer keeps its anchor until
release; trimming resumes afterward. A reader parked mid-history retains every
loaded row, so deliberate deep-history reading can still grow memory and DOM.

Timeline snapshots merge with provisional streaming items by `item_id` (checkpoint
coordinates `<msg_idx>.<block_idx>` for native agents, byte-offset coordinates
`h<offset>.<n>` for record-backed external harness agents). Unanswered external
tool calls (`activity_pending: true`) are replaced in place when a later snapshot
delivers the settled call, or dropped when a newer snapshot omits them.

## Overlay rows (external harness streaming)

An external harness's record has no token-level content, so while it writes a block
the backend streams it as ordinary `chat_*` / `reasoning_*` events whose ids are
`live.<turn_id>.<block_key>`. These overlay rows are display only and never a record
position: they sort after every record row, and `isLiveOverlayId` /
`isDisplayOnlyItemId` (`lib/fold/item-id.ts`) keep them out of reading-position
bookmarks and the tail-departure watermark, the unread boundary, publication
matching (`withUserMessages`) and the older-page cursor.

A `timeline_snapshot` may carry `retires: string[]`, the overlay ids it supersedes
(absent when empty). `mergeSnapshotWithStreaming` removes them in the same step it
applies `items`, so a committed row and its overlay never show together; a snapshot
with `items: []` and `retires` ends a turn or an interrupt. Retirement is by id, so
it also removes an overlay whose committed row a `GET /timeline` already delivered.
An overlay id that no snapshot retires is kept like any in-flight partial, and the
backend sends nothing for an id after retiring it.

An overlay never outlives its turn even when its retirement never reached the page (a
dropped event, an SSE reconnect, a replaced host): `settleTurn` and `cancelled` remove
every overlay row, a `GET /timeline` merged while no turn runs does the same
(`dropLiveOverlays`, `lib/fold/overlay-id.ts`), and `hasUnsettledClock` counts an
overlay row so an idle page always settles. Overlay rows are client-clocked, so the
Working/Worked segments (`computeSegment`) are bounded by the record's own rows as soon as
the segment holds one with a timestamp; before that the overlay rows anchor the live clock.

## Earlier harness epochs

An agent that switched harness keeps one timeline. The gateway reads the current
epoch exactly as before (unprefixed ids, live SSE and overlay ids unchanged) and
serves earlier epochs only through `before=` paging, with every id prefixed
`e<seq>.` (`e0.h123.0`, `e0.12.0`, `e0.s1.<cp>.3.0`). The synthetic local `e<seq>.sw`
(the switch block) marks an epoch's start; an epoch whose record cannot be read is
skipped by the gateway and leaves no row.

The store order is one total order, `(epoch, slot, compact rank, message, block)`
with the current epoch last (`sortByItemId`, `lib/fold/item-id.ts`); no timestamp
decides placement. The compact machinery (`parseItemIdParts`, transition buffers,
`crossedUnseenCompact`, the `msg_count` stale-partial rule) only understands
current-epoch ids: `parseItemIdParts` returns null for a prefixed id, so earlier
epochs are never judged by a compact or by `msg_count`. The older-page cursor is the
oldest loaded real row, which may be the switch block, and the gateway accepts every
id it emits as a cursor.

`useTimeline` takes the agent's harness; when it changes for the selected agent the
view drops every row (the rows on screen would otherwise sit beside the same history
now prefixed `e<seq>.`) and one composed read re-seeds the new head.

## Relationship to Other Nodes

- [Frontend State Management](frontend-state.md)
