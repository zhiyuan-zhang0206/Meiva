---
type: doc
title: Timeline deep collapse
description: Turn grouping and expansion state — secondary-item classification into TurnBlocks, Details-mode defaults, and per-turn and per-item override maps.
tags:
- frontend
---

# Timeline deep collapse

`runs.classifyItem` classifies items as primary (user-facing agent replies + human inbound, always visible directly in flow without a card-toggle header) / secondary (thinking / code / output, non-user-facing `agent_chat` with `user_facing === false` from background wakes, inter-agent messages, system inbound, compact, system_prompt, note marker) / bare (ephemeral marker). Non-user-facing `agent_chat` items carry `fixedDefault: false` inside `TurnBlock` so expanding `Details` in `None` mode keeps them collapsed until clicked, while still offering inline `Copy` when expanded. Adjacent secondary items form a `TurnBlock`; historical turns collapse by default unless Details All is active, while an active trailing turn (`turnActive` true) opens as the fixed-height (`h-56`) Live Activity Window (`PendingLiveActivityWindow` holds the same footprint immediately after a human message before the first work item lands). In None mode a manually opened historical turn keeps its nested blocks collapsed (each opens on its
own click); All/Last cascade the expansion into revealed children (task #4780). While the settings query is in flight the
view renders the collapsed state regardless of the reported mode — a stale
`all` read would flash every block open on each cold load, and the reveal when
the real value lands is the lesser evil. A turn's expanded state is a pinned map
keyed by the turn id (its first member's `item_id`, stable across streaming
commits): a click flips the currently rendered state and pins it, so the choice
survives defaults that flip on their own — most notably the active final turn
auto-collapsing when the agent goes idle (#510); absence from the map follows
the mode. Per-item overrides are the opposite-of-default set (membership means
flipped); a mode change clears both maps, and re-picking the same mode bumps a
reset token so manual expansions under that mode revert too (user ruling
2026-08-06). Pinned state is scoped per thread — item ids are message indexes
local to each thread, so an unscoped map would resurrect another conversation's
choices. Copy joins a primary message's timestamp in the hover band under it for
settled human bubbles and agent replies (and renders inline at top-right on
expanded inter-agent messages — see [[ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md|card chrome]]); thinking, tool calls and notes carry none.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]] — the renderer whose turn grouping and expansion this node owns.
