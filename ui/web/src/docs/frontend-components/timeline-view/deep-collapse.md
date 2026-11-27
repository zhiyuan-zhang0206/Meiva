# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking including `llmWaiting` keepalives / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, and all rendered system markers including background tasks, errors, retry/status notices, and unrecognized sources), while `isSilentTimelineItem` skips non-rendering items (`compact_done`, `cancelled`, `status:Cancellation requested.`, `compact_request:*`, and hidden `[files]` companions) so they never split a turn or add empty vertical gaps — note markers are no longer among them: every `NoteTag` the agent read now renders (user ruling 2026-11-09). A non-user-facing chat item remains in Details even when it is the latest output. All adjacent secondary items — including across compact summaries, historical compact boundaries, and live transition buffers — merge into one `TurnBlock`. Turns start collapsed unless the final turn is active and Display → Timeline `display.expand_working_block` is enabled. An opened turn's body flows at its natural height in the timeline — no inner `ScrollArea`, no viewport-relative cap, whether it is historical or the live one (user ruling 2026-11-09, revoking the 2026-11-07 half-viewport rule and its full-height successor). A finished block is as tall as the work really was and the reader scrolls the timeline itself (expanding a turn calls `notifyBlockExpanded` so layout growth never pins the viewport to the tail); the ongoing block follows the scroll rules described in [The expanded live block](live-block.md). Expanded turn headers stick below HeaderBar with an opaque, square-cornered
surface, 10px vertical padding, and 4px between header rows. Collapsed
headers keep their rounded corners and 2px vertical padding. Hover and keyboard focus highlight the
title text. On hover-capable devices the collapsed chevron appears on hover or
keyboard focus. It stays visible while expanded, and on touch devices where
hover is unavailable. The chevron reserves its space immediately after the
title text. The expanded body uses indentation to group its contents. Primary
message headers keep their existing flow. Individual detail headers use the same
chevron and title highlight in normal flow.

Inside an opened `TurnBlock`, live (`Working`) or settled (`Worked`) alike, every
item starts collapsed except the agent's text output (`agent_chat`,
`CardConfig.fixedDefault`; user ruling 2026-11-22). Attachments, peer/external/system
inbound messages (categorized by source into `Shell`, `Watcher`, `Schedule`, or
`System`), system prompts, thinking, code, output, and all non-silent `system_marker`
cards (`Note`, `Memory`, lifecycle, `Background task`, `System notice`,
`Error`, `Unrecognized system_marker`) start collapsed and open individually on
click. The live turn has no rule of its own.

Secondary item headers omit token/line count suffixes (`Thought for Xs`, `Ran in Xs`, `System prompt`) and system markers (`Memory`, `Note`, lifecycle) use the same borderless monochrome header chrome without colored accent overrides (user ruling 2026-11-01). A turn's expanded state is a pinned map
keyed by a reconciled turn key (`useTimelineGroupWindow` matches surviving
members from newest to oldest within the same thread so `virtualKey` and manual
overrides stay stable across both streaming commits and front-of-turn
`prependOlder` / `trimOldestWhileFollowing` mutations, while `data-item-id` and
`data-turn-id` continue reflecting the current first member's `item_id`): a click flips the currently rendered state and pins it, so the choice
survives defaults that flip on their own — including the final turn settling or becoming historical; absence from the map follows
the historical/trailing default. Per-item overrides also pin the state the reader picked, including when the
live turn settles. Pinned state is scoped per thread — item ids are
message indexes local to each thread, so an unscoped map would resurrect another
conversation's choices. Copy joins a primary message's timestamp in the hover
band under it for settled human bubbles and agent replies (and renders
right-aligned below the expanded body on copyable secondary messages — see
[card chrome](card-chrome.md)); thinking, tool calls and notes carry none.

## Turn timer and single-line collapsed strip

The collapsed turn strip shows a single status line. Expanded turns show the counts of turns, messages and other items on a separate line beneath the status, followed by SDK-call chips and the detail rows where present. While `turnActive` is true, the trailing `TurnBlock` is the live window (`data-live-activity-window="true"`, using the timeline as its scrollport). Before the first work item arrives, an active qualified source shows an unboxed inline `PendingLiveActivityWindow` (`Working for Xs` header row without a disclosure chevron) when the timeline is empty or ends on a human prompt. Its suppression during source and reader loading is owned by [the loading ring](loading-ring.md).

**A queued message is not work in progress.** `inbound_arrived` makes `turnActive` true at once (the composer must turn into Stop), but the message then sits in the pending strip until the agent claims it and it reaches the timeline — for an external harness, until the provider acknowledges it. The fold also tracks `turnRunning` (set only by the agent's own `*_start` / `llm_keepalive` events, cleared at turn end) and the inbound ids that arrived while no turn ran. `awaitingPickup` is true while no turn runs and none of those ids is on the timeline yet; Home then hands `TimelineView` `turnActive = false`, so neither the `PendingLiveActivityWindow` (which would sit under an older prompt and count from its `created_at`) nor the last `TurnBlock` (which would light up although it finished long ago) presents work. Any one arrived message on the timeline makes it the turn's trigger even while later ones still queue, and a turn that is really running (`turnRunning`) stays lit when more messages queue behind it. After a reload nothing is known about queued messages, so the roster's `running` status alone presents work as before. `tests/e2e/visual/test_pending_pickup.py` drives the scripted stream through arrival and claim in a real browser, for an empty timeline, a stale last prompt and an earlier work block.

The work-block timer sums the active segments inside the block while excluding
inter-turn idle gaps (user ruling 2026-11-22; derived purely on the frontend
from timeline items, with no backend sidecars):

- **Active segment splitting** (`runs.summarizeTurn`): after stripping standing
  context items (`system_prompt` and `system_marker` with `show_timestamp:
  false`), the remaining event items split into contiguous segments whenever:
  1. A segment closed on a hidden `agent_chat` (`user_facing: false`) and the
     next item is not a same-step agent output (same-step tool calls such as
     `h100.0` hidden text + `h100.1` `agent_code` stay in the same segment and
     reopen it so subsequent `code_output` is not split), or closed permanently
     on a turn-closing lifecycle marker (`lifecycle_terminate`,
     `lifecycle_interrupt`).
  2. A segment already contains agent output (`agent_reasoning` including
     `llmWaiting`, `agent_code`, `code_output`, `attach`, `agent_chat`) and a
     new inbound wake arrives (`inbound_chat`, `inbound_compact_request`,
     `inbound_compact_summary`, or a wake `system_marker`: `heartbeat`,
     `lifecycle_restart`, `lifecycle_resurrect`, `lifecycle_fork`).
  3. A segment has no agent output yet and another `inbound_chat` or wake
     `system_marker` arrives (consecutive queued wakes without agent output
     close the empty segment at `0ms` so queue waiting time is never counted,
     while `inbound_compact_request` → `inbound_compact_summary` remains one
     segment).
  4. Any non-wake `system_marker` (`show_timestamp: true`, e.g., an archived note,
     `new_skills`, `compact_failed`, `turn_lost`, or ephemeral `error:` /
     `status:`) appearing while idle, after a closed segment, or before agent
     output lands as its own closed `0ms` segment (`hasAgentOutput: false,
     closed: true`) so it never absorbs a subsequent hidden wake's agent output;
     `lifecycle_terminate` / `lifecycle_interrupt` after agent output caps and
     hard-closes that segment.
- **Worked** = sum of `max(segEnd - segStart, 0)` across all segments that
  contain agent output (`settledActiveMs`). Empty inbound-only or marker-only
  segments contribute `0` and never taint the block if their own timestamps are
  absent. For example, 10m of work, 1h idle, then 10m of work displays
  `Worked for 20m` rather than `80m`.
- **Working** = `priorActiveMs + max(now - liveAnchor, 0)`, where
  `priorActiveMs` is the lower bound formed by summing the measurable
  durations of completed active segments prior to the trailing open segment (a
  segment whose duration cannot be measured contributes `0`, so a single
  unmeasured earlier segment never silences the clock) and `liveAnchor` is the
  trailing open segment's start timestamp (`liveSegmentStartMs`). When the
  trailing segment is already closed (for example, after a hidden `agent_chat`,
  `lifecycle_terminate`, or trailing non-wake marker while a hidden heartbeat
  has just woken before its first item lands) or lacks a start timestamp,
  completed active segments roll into `priorActiveMs` and `liveAnchor` falls
  back to the `lastEventAt` prop (fed from `timelineStore.liveAnchorAt`, which
  is maintained only while `turnActive` is `true` and reset to `0` on
  `settleTurn`, terminal SSE events, and connection close so a later wake in the
  same tab never inherits a stale pre-idle anchor), and finally to the instant
  the header first saw the turn active. The last fallback only undercounts and
  never absorbs idle time.

**Invariant (user report 2026-11-25):** whenever `turnActive` is true and a
Working header is visible (`TurnBlock` or `PendingLiveActivityWindow`), it reads
`Working for Xs` and keeps counting every second; there is no static `Working…`
state and no `working` message. Typical triggers that used to freeze it:
external-harness tool calls without duration fields, a one-item earlier turn
(for example only an `agent_chat`) inside the same work block, a live segment
with no timestamps, and an empty timeline whose prompt carries no `created_at`.
`tests/e2e/visual/test_working_clock.py` drives the scripted stream in a real
browser through a native work block with an earlier one-item turn and an
external-harness block without duration fields, and asserts the header advances.

Item boundaries come from the timestamps and durations timeline items already
carry (`created_at` plus `reasoning_ms` / `code_elapsed_ms` / `exec_ms`, where
committed items with a duration stamp `created_at` at completion so start is
`created_at - duration`, and live SSE items carry `*StartedAt`). Standing
context items (`system_prompt` and `system_marker` with `show_timestamp: false`)
are excluded so re-attached head notes never shift a segment's start. Before the
first item of a live work block arrives, `PendingLiveActivityWindow` anchors on
the triggering human prompt's `created_at` (or `timelineStore.liveAnchorAt`,
passed as `lastEventAt`, on an empty timeline, or the instant it first rendered). Because the calculation depends
only on the timeline items themselves, live and settled readings agree across
reloads, reconnects and rebuilt harness bridges without per-turn timing sidecars
or turn-ID deduplication.

The active header reads "Working for Xs" and settles to "Worked" /
"Worked for Xs". Timestamped spans below one second display as "1s" ("Working
for 1s" / "Worked for 1s", user ruling 2026-11-21); detail chips retain
sub-second precision. An active segment whose settled items have no measured
duration field (`reasoning_ms`, `reasoningElapsedMs`, `code_elapsed_ms`,
`codeElapsedMs`, `exec_ms`, `execElapsedMs`) and zero or non-positive wall-clock
span (`endMs <= startMs`, such as a single unmeasured reasoning item or
same-second items) reports `durationMs = null` so the header shows plain
"Worked" instead of fabricating "Worked for 1s" (whereas a genuine positive
sub-second span `endMs > startMs` or an explicit `0ms` measurement still
displays "Worked for 1s"). When any active segment's start or end timestamp is
truly missing, the settled header likewise shows plain "Worked" with no duration
and no per-block warning icon (the live header keeps counting as described
above).

Both `PendingLiveActivityWindow` and active (`turnActive`) `TurnBlock` title spans
share the sidebar's Silver Glint sweep (`.timeline-title-working` sharing
`.agent-title-working` / `agent-title-sweep` and locking `startTime = 0` via
`useSyncedShimmer`), crossing their text in step with the sidebar titles, staying in phase across the handoff and settling back to
static text without shimmer once the turn completes; the leading `Layers` icon,
disclosure chevron, and expanded summary remain unshimmered.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer whose turn grouping and expansion this node owns.
