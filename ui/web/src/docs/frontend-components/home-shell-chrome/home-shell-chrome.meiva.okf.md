---
type: doc
title: Home shell chrome
description: The home columns' title bars and where the timeline's Details mode lives now.
tags:
- frontend
---

# Home shell chrome

## Column title bars

The three home columns — Agent Tree, Timeline, right panel — each carry a title
bar `BAR_HEIGHT_PX` (44px) tall that paints `BAR_DIVIDER_CLASS`: a 1px rule inset
4px from both ends, so the three read as three separate rules on one line rather
than as one page-wide divider. The right panel's document bar (the "Updated …"
strip) is such a bar, not a section header — it takes the wrapping variant
(`COLUMN_TITLE_BAR_WRAP_CLASS`: the same 44px as a minimum, allowed to grow only
when its content cannot fit one line), so at desktop widths its rule ends on the
same line as its neighbours. The notice header above it takes the fixed variant
for the same reason. Both variants are `relative`: the rule is the bar's own
`after` element, painted against the bar and not against the section around it.

Every home column title bar is BAR_HEIGHT_PX tall and paints BAR_DIVIDER_CLASS's inset rule, so the Agent Tree, Timeline and right-panel dividers sit on one line; the right panel's document bar takes the wrapping variant (44px minimum, growing only when its content cannot fit one line) and its notice header takes the fixed variant — neither draws a border on its section.

## Details mode

The Details mode has one writer and one surface: the Display settings row bound to
`display.expand_runs_mode` (`/control#display-timeline`, "Timeline details"), which
keeps all three modes (All / Last / None) reachable. Picking a mode — including the
one already stored — re-applies it and collapses manual expansions. Because the
picker's page unmounts the timeline, the reset is recorded as a pending flag in
`lib/layout/content-toggle-store.ts` and consumed by whichever timeline mounts
next; the token alone could not survive that gap.

The composer carries no Details affordance: the read-only summary chip was
removed on 2026-10-18, so nothing on the home screen renders the mode or writes
it. The chip's absence is deliberate — a control that cannot change the value is
not worth a row in the composer.
