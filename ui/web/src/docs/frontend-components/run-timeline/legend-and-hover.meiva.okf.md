---
type: doc
title: Agent view legend and hover
description: Category legend toggles, class and source highlights, and canvas hover lighting.
tags: [frontend]
---

# Agent view legend and hover

**Legend highlight.** Each legend entry is a toggle (`run-timeline-legend.tsx`): pressing it highlights every block of that class (`Highlight`: class plus optional source) and fades the rest — other blocks and all summary blocks drop to 0.12 opacity (a selected block keeps its ring). The state lives on the page, so zoom and pan keep it. While an inbound class (human / agent) is highlighted and its blocks come from several senders, a select narrows it to one source (`agent:N` reads "Inbound from agent N"). The context breakdown card's category rows that stand for a block class (user input, agent messages, thinking, text output, tool calls, tool responses, system notes) are the same toggle (`classCategory` / `categoryClass`).

**Hover.** Hover only highlights the candidate and its relationships; there is no visible text readout above the canvas. Hovering a block softly lights its ancestor chain; hovering a node lights its chain and the blocks its message span covers (`hoverLit`). A selection's own lighting wins over hover. Details stay in the side panel, and `model/run-timeline-readout.ts` announces committed selections to assistive technology without reacting to hover.

Parent: [[ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md]].
