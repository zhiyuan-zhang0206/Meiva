---
type: doc
title: Timeline deep collapse
description: Turn grouping and expansion state — secondary-item classification into TurnBlocks, settled vs live expansion defaults, and per-turn and per-item override maps.
tags:
- frontend
---

# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking including `llmWaiting` keepalives / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (visible error or unrecognized ephemeral marker), while `isSilentTimelineItem` skips non-rendering items (`compact_done`, `cancelled`, `compact_request:*`, `non_human_wake` recency notes, and hidden `[files]` companions) so they never split a turn or add empty vertical gaps. A non-user-facing chat item remains in Details even when it is the latest output. All adjacent secondary items — including across compact summaries, historical compact boundaries, and live transition buffers — merge into one `TurnBlock`. Turns start collapsed unless the final turn is active and Display → Timeline `display.expand_working_block` is enabled. When a turn followed by another timeline group is opened, its body flows at full height in the outer timeline (user ruling 2026-11-07). Only the final timeline group retains the half-viewport inner `ScrollArea` (`--timeline-turn-max-h`). An opened active turn expands its children; historical child details retain their individual defaults and manual toggles. Non-user-facing agent text and attachments default open inside an expanded historical turn, while machinery details default collapsed. Expanded turn headers stick to the timeline viewport top until their own turn ends; an opaque background keeps scrolling details from showing through. Primary message and individual detail headers keep their existing flow.

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
[[ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md|card chrome]]); thinking, tool calls and notes carry none.

## Turn timer and single-line collapsed strip

The collapsed turn strip shows a single status line. Expanded turns show the counts of turns, messages and other items on a separate line beneath the status, followed by SDK-call chips and the detail rows where present. While `turnActive` is true, the trailing `TurnBlock` is the live window (`data-live-activity-window="true"`, feeding its inner detached state into the single outer `ScrollToBottomButton`), preceded by an unboxed inline `PendingLiveActivityWindow` immediately after a human prompt before the first work item arrives.

The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the active `TurnBlock` header ("Working for Xs" / "Working…") displaying `totalWorkedMs` (`workedMs` plus the in-flight block's elapsed) and transitioning cleanly to "Worked for Xs" when the turn completes. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the renderer whose turn grouping and expansion this node owns.
