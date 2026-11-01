---
type: doc
title: Timeline deep collapse
description: Turn grouping and expansion state — secondary-item classification into TurnBlocks, settled vs live expansion defaults, and per-turn and per-item override maps.
tags:
- frontend
---

# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking including `llmWaiting` keepalives / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (visible error or unrecognized ephemeral marker), while `isSilentTimelineItem` skips non-rendering items (`compact_done`, `cancelled`, `compact_request:*`, `non_human_wake` recency notes, and hidden `[files]` companions) so they never split a turn or add empty vertical gaps. As a frontend-only presentation fallback (`isFallbackVisibleAgentChat` / `findLastVisibleAgentChat` in `runs.ts` and `segments.ts`), the timeline's latest non-empty `agent_chat` (excluding half-streamed `Meiva agent to user:` headers whose body has not arrived yet) is always promoted to a top-level primary assistant row even when `user_facing === false`, refolding automatically into its historical `TurnBlock` once a newer non-empty `agent_chat` arrives without mutating backend `user_facing`. All adjacent secondary items — including across compact summaries, historical compact boundaries, and live transition buffers — merge into one `TurnBlock`; both historical turns ("Worked for Xs") and active trailing turns (`turnActive` true, "Working for Xs" / `PendingLiveActivityWindow`) share one unboxed inline `TurnBlock` presentation whose expanded body sits inside an auto-hiding `ScrollArea` capped at 50% of the visible timeline message area (`--timeline-turn-max-h`, user ruling 2026-11-01). Settled historical turns always start collapsed, while an active trailing turn (`isLiveTurn`) opens automatically and expands all of its child blocks so live progress streams visibly (user ruling 2026-11-01). Opening a finished historical turn keeps machinery blocks (`fixedDefault: false`) collapsed until clicked, whereas non-user-facing `agent_chat` (`user_facing === false`) and `attach` carry `fixedDefault: true` so the agent's normal text output and attachments default expanded inside an opened finished `TurnBlock`, with non-user-facing `agent_chat` offering a right-aligned bottom `Copy` button below its expanded body (user ruling 2026-11-01). Secondary item headers omit token/line count suffixes (`Thought for Xs`, `Ran in Xs`, `System prompt`) and system markers (`Memory`, `Note`, lifecycle) use the same borderless monochrome header chrome without colored accent overrides (user ruling 2026-11-01). A turn's expanded state is a pinned map
keyed by the turn id (its first member's `item_id`, stable across streaming
commits): a click flips the currently rendered state and pins it, so the choice
survives defaults that flip on their own — most notably the active final turn
auto-collapsing when the agent goes idle (#510); absence from the map follows
the settled/live default. Per-item overrides are the opposite-of-default set
(membership means flipped). Pinned state is scoped per thread — item ids are
message indexes local to each thread, so an unscoped map would resurrect another
conversation's choices. Copy joins a primary message's timestamp in the hover
band under it for settled human bubbles and agent replies (and renders
right-aligned below the expanded body on copyable secondary messages — see
[[ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md|card chrome]]); thinking, tool calls and notes carry none.

## Turn timer and single-line collapsed strip

The collapsed historical turn strip is one fixed-height line: the summary truncates and the SDK-call chips are expanded-only, because a block carrying chips stood up to 38px taller than one without and those strips sit between messages (user report 2026-10-21). While `turnActive` is true and the trailing group is a `TurnBlock`, it defaults open (`data-live-activity-window="true"`, feeding its inner detached state into the single outer `ScrollToBottomButton`), preceded by an unboxed inline `PendingLiveActivityWindow` immediately after a human prompt before the first work item arrives.

The turn header's timer reads ONE basis in both states — `summarizeTurn.workedMs`, the sum of the turn's block durations — with the active `TurnBlock` header ("Working for Xs" / "Working…") displaying `totalWorkedMs` (`workedMs` plus the in-flight block's elapsed) and transitioning cleanly to "Worked for Xs" when the turn completes. Wall-clock across a turn is never displayed: a turn is a maximal run of secondary items and can span an idle gap (a restart, a wake-up the agent had not picked up yet).

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the renderer whose turn grouping and expansion this node owns.
