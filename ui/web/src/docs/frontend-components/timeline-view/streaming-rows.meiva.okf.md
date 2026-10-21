---
type: doc
title: Timeline streaming rows
description: Memoized row rendering, shared clocks and streaming announcements.
tags: [frontend]
---

# Timeline streaming rows

During streaming each line is memoized `TimelineRow` + `React.memo` PythonCode/ChatMarkdown to suppress unchanged-row renders; `TimelineView` itself is memoized against unrelated page renders, and `TurnBlock` compares its rebuilt member ids and summary by value so collapsed historical turns skip each regroup. Every live clock (block chips, the turn header, the compacting block) shares one 100ms ticker in `reasoning-clock`; the interval exists only while a clock is live, and block clocks tick only while the agent is busy (`LiveClockGate`). The load-older circle animates while the reader pulls or a page is loading, and is absent while idle. Grouping is reused while the items reference is unchanged. Rows render with `aria-live="off"` (rows and history dividers opt out individually, so the direct-child structure used by scroll anchoring and dividers is unchanged); the view announces only turn-boundary changes, so streamed chunks generate no screen-reader noise. A live stamp that outlived a missed turn-end freezes on an idle page instead of re-rendering its row ten times a second. Sticky bottom auto-scroll.

See [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
