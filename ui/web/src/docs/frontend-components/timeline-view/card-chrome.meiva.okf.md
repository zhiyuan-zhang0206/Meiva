---
type: doc
title: Timeline card chrome
description: Primary timeline message presentation, publication acknowledgment, and Copy reveal rules for Details.
tags:
- frontend
---

# Timeline card chrome

`TimelineRow` renders human messages as right-aligned bubbles and user-facing
agent replies as unboxed Markdown. Primary messages have no floating footer.
Publications are marked read when an invisible anchor at the bottom of their
row enters the viewport while the document is visible. The anchor occupies no
layout space.

Expanded Details offer an inline Copy control at the bottom right of message
text from an agent or external agent. The per-kind whitelist in
`messageCardConfig` excludes thinking, tool calls and outputs, attachments,
system prompts, and system notes. The Copy control appears on hover or focus.
On touch, it appears for `TOUCH_REVEAL_MS` after touching the card. The hidden
control is click-through and remains reachable by keyboard.

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].

### Silent model keepalives (`llm_keepalive`)

An empty `llm_keepalive` creates a transient `agent_reasoning` item (`llmWaiting: true`, empty payload) for its message position when no real block exists. It classifies as secondary (`runs.classifyItem`) so it folds inside the active `TurnBlock` rather than breaking out into a standalone block, renders with the reasoning card's live `"Thinking for {duration}"` clock presentation (`ThinkingSummary`), and suppresses an empty body wrapper when expanded (`TimelineRow`). Repeated heartbeats keep the same item reference and never append text; real content, terminal events and committed snapshots replace or remove it.
