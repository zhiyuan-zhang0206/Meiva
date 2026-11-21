# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking including `llmWaiting` keepalives / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, and all rendered system markers including errors, retry status and harness notices), while `isSilentTimelineItem` skips non-rendering items (`compact_done`, `cancelled`, `status:Cancellation requested.`, `compact_request:*`, and hidden `[files]` companions) so they never split a turn or add empty vertical gaps — note markers are no longer among them: every `NoteTag` the agent read now renders (user ruling 2026-11-09). A non-user-facing chat item remains in Details even when it is the latest output. All adjacent secondary items — including across compact summaries, historical compact boundaries, and live transition buffers — merge into one `TurnBlock`. Turns start collapsed unless the final turn is active and Display → Timeline `display.expand_working_block` is enabled. An opened turn's body flows at its natural height in the timeline — no inner `ScrollArea`, no viewport-relative cap, whether it is historical or the live one (user ruling 2026-11-09, revoking the 2026-11-07 half-viewport rule and its full-height successor). A finished block is as tall as the work really was and the reader scrolls the timeline itself (expanding a turn calls `notifyBlockExpanded` so layout growth never pins the viewport to the tail); the ongoing block follows the scroll rules described in [The expanded live block](live-block.md). An opened active turn expands its work details for live reading. Messages from peer agents, external agents and CLI sub-agents start collapsed in both live and settled turns and can be opened individually. Non-user-facing agent text, attachments, system inbound messages and note markers default open in settled turns. Other settled details, including thinking, code, output and system prompts, default collapsed. Expanded turn headers stick to the timeline viewport top until their own turn ends; an opaque background keeps scrolling details from showing through. The header
keeps the same surface in both states. Hover and keyboard focus highlight the
title text. On hover-capable devices the collapsed chevron appears on hover or
keyboard focus. It stays visible while expanded, and on touch devices where
hover is unavailable. The chevron reserves its space immediately after the
title text. The expanded body uses indentation to group its contents. Primary
message headers keep their existing flow. Individual detail headers use the same
chevron and title highlight in normal flow.

Inside an opened `TurnBlock`, live (`Working`) or settled (`Worked`) alike, every
item starts collapsed except the agent's text output and attachments
(`CardConfig.fixedDefault`; user ruling 2026-11-20). The live turn has no rule of
its own.

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

The collapsed turn strip shows a single status line. Expanded turns show the counts of turns, messages and other items on a separate line beneath the status, followed by SDK-call chips and the detail rows where present. While `turnActive` is true, the trailing `TurnBlock` is the live window (`data-live-activity-window="true"`, using the timeline as its scrollport). Before the first work item arrives, an active qualified source shows an unboxed inline `PendingLiveActivityWindow` (`Working…` header row without a disclosure chevron) when the timeline is empty or ends on a human prompt. Its suppression during source and reader loading is owned by [the loading ring](loading-ring.md).

The work-block timer measures the block's wall-clock span rather than summing
per-turn active durations (user ruling 2026-11-21):

- **Working** = `now` − the start of the first event item in this work block.
  If earlier turns finished without a user-visible reply and a new turn starts
  in the same work block, the live timer continues from the first item's start
  across the intervening time.
- **Worked** = the end of the last settled event item in the block − the start
  of its first event item.

Item boundaries come from the timestamps and durations timeline items already
carry (`created_at` plus `reasoning_ms` / `code_elapsed_ms` / `exec_ms`, where
committed items with a duration stamp `created_at` at completion so start is
`created_at - duration`, and live SSE items carry `*StartedAt`). Standing
context items (`system_prompt` and `system_marker` with `show_timestamp: false`)
are excluded so re-attached head notes never shift a block's start. Before the
first item of a live work block arrives, `PendingLiveActivityWindow` anchors on
the triggering human prompt's `created_at` (or `liveAnchorAt` on an empty
timeline). Because the calculation depends only on the timeline items themselves,
live and settled readings agree across reloads, reconnects and rebuilt harness
bridges without per-turn timing sidecars or turn-ID deduplication.

The active header remains "Working…" / "Working for Xs" and settles to "Worked" /
"Worked for Xs". Timestamped spans below one second display as "1s" ("Working
for 1s" / "Worked for 1s", user ruling 2026-11-21); detail chips retain
sub-second precision. When a block's start or end timestamp is truly missing,
the header shows plain "Worked" ("Working…" while active, or `liveAnchorAt`
fallback when live) with no duration and no per-block warning icon.

Both `PendingLiveActivityWindow` and active (`turnActive`) `TurnBlock` title spans
share the sidebar's Silver Glint sweep (`.timeline-title-working` sharing
`.agent-title-working` / `agent-title-sweep` and locking `startTime = 0` via
`useSyncedShimmer`), crossing their text in step with the sidebar titles, staying in phase across the handoff and settling back to
static text without shimmer once the turn completes; the leading `Layers` icon,
disclosure chevron, and expanded summary remain unshimmered.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer whose turn grouping and expansion this node owns.
