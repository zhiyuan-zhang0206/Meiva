# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking including `llmWaiting` keepalives / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (visible error or unrecognized ephemeral marker), while `isSilentTimelineItem` skips non-rendering items (`compact_done`, `cancelled`, `status:Cancellation requested.`, `compact_request:*`, and hidden `[files]` companions) so they never split a turn or add empty vertical gaps — note markers are no longer among them: every `NoteTag` the agent read now renders (user ruling 2026-11-09). A non-user-facing chat item remains in Details even when it is the latest output. All adjacent secondary items — including across compact summaries, historical compact boundaries, and live transition buffers — merge into one `TurnBlock`. Turns start collapsed unless the final turn is active and Display → Timeline `display.expand_working_block` is enabled. An opened turn's body flows at its natural height in the timeline — no inner `ScrollArea`, no viewport-relative cap, whether it is historical or the live one (user ruling 2026-11-09, revoking the 2026-11-07 half-viewport rule and its full-height successor). A finished block is as tall as the work really was and the reader scrolls the timeline itself (expanding a turn calls `notifyBlockExpanded` so layout growth never pins the viewport to the tail); the ongoing block follows the scroll rules described in [The expanded live block](live-block.md). An opened active turn expands its children; historical child details retain their individual defaults and manual toggles. Everything the agent read defaults open inside an expanded historical turn — non-user-facing agent text, attachments, other agents' and system inbound messages, and note markers — while machinery details (thinking, code, output, compact envelopes, the system prompt, lifecycle and memory markers) default collapsed. Expanded turn headers stick to the timeline viewport top until their own turn ends; an opaque background keeps scrolling details from showing through. The header
keeps the same surface in both states. Hover and keyboard focus highlight the
title text. On hover-capable devices the collapsed chevron appears on hover or
keyboard focus. It stays visible while expanded, and on touch devices where
hover is unavailable. The chevron keeps its layout space in both states. The
expanded body uses indentation to group its contents. Primary message and
individual detail headers keep their existing flow.

Secondary item headers omit token/line count suffixes (`Thought for Xs`, `Ran in Xs`, `System prompt`) and system markers (`Memory`, `Note`, lifecycle) use the same borderless monochrome header chrome without colored accent overrides (user ruling 2026-11-01). A turn's expanded state is a pinned map
keyed by a reconciled turn key (`useTimelineGroupWindow` matches surviving
members from newest to oldest within the same thread so `virtualKey` and manual
overrides stay stable across both streaming commits and front-of-turn
`prependOlder` / `trimOldestWhileFollowing` mutations, while `data-item-id` and
`data-turn-id` continue reflecting the current first member's `item_id`): a click flips the currently rendered state and pins it, so the choice
survives defaults that flip on their own — including the final turn settling or becoming historical; absence from the map follows
the historical/trailing default. Per-item overrides are the opposite-of-default set
(membership means flipped). Pinned state is scoped per thread — item ids are
message indexes local to each thread, so an unscoped map would resurrect another
conversation's choices. Copy joins a primary message's timestamp in the hover
band under it for settled human bubbles and agent replies (and renders
right-aligned below the expanded body on copyable secondary messages — see
[card chrome](card-chrome.md)); thinking, tool calls and notes carry none.

## Turn timer and single-line collapsed strip

The collapsed turn strip shows a single status line. Expanded turns show the counts of turns, messages and other items on a separate line beneath the status, followed by SDK-call chips and the detail rows where present. While `turnActive` is true, the trailing `TurnBlock` is the live window (`data-live-activity-window="true"`, using the timeline as its scrollport), preceded by an unboxed inline `PendingLiveActivityWindow` immediately after a human prompt before the first work item arrives.

The turn header's timer reads one basis — `summarizeTurn.workedMs`, the sum of
the turn's block durations, which a turn that spans an idle gap (a restart, a
wake-up the agent had not picked up yet) must not inflate — plus one live
stretch. The active `TurnBlock` header ("Working for Xs" / "Working…") displays
`totalWorkedMs` and settles into "Worked for Xs" when the turn completes.

What the live stretch counts changed (user report 2026-11-08, an Antigravity turn
stuck at "Working for 1m 2s" with nothing left to advance it). It used to be the
in-flight block's elapsed time only, so between two blocks — a tool result
committed, the model thinking before its next token, a harness step transition —
nothing was live and the number froze for as long as the gap lasted. The anchor
is now the in-flight block's start while one runs, and otherwise
`liveAnchorAt` — the page's own clock of the last event the thread received
(`useTimelineStore.liveAnchorAt`), which is precisely the moment the current
unaccounted stretch began. It comes from the browser's clock, never an
agent-host timestamp, so a skew between the two cannot move the number; it resets
on a thread switch. While a turn is open the number therefore only moves forward,
and it lands on the measured sum when the next block commits.

## Relationship to Other Nodes

- [Timeline View](timeline-view.md) — the renderer whose turn grouping and expansion this node owns.
