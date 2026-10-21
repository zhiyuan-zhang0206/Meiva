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

The conversation title includes the agent's stored Harness display name, using
the same labels as the spawn picker, in every mode: Inspector changes what the right
column shows, so naming the bar after the mode would hide the agent the reader is still
in (user 2026-10-21). The mode is visible in its pressed toggle and in the column itself.

## Edge-pinned controls

A control that belongs to the viewport's edge belongs to the bar, not to the
centred column. `HeaderBar` pins the collapsed rail's restore button at `left-2`
and accepts an optional `pinnedRight` node at `right-2`, both absolutely
positioned and vertically centred; the conversation column is capped and
centred, so an in-flow control lands near the middle of a wide viewport and
reads as part of the timeline. The home page passes the collapsed right panel's
reopen control through `pinnedRight`. Both slots are desktop-only (`md:`),
matching the rail behaviour they mirror.

The collapsed rail's restore control and a collapsed right panel's reopen control are pinned to HeaderBar's own edges, never to the centred conversation column.

The restore control also carries the unresolved-alert count (`alertCount`, a count bubble at its top-right corner, nothing at zero) and names it in its label: the rail is the one surface that disappears when collapsed, so the count that leads the sidebar's icon row would otherwise vanish with it (user 2026-10-21). It is the only signal the collapsed state shows — no agent list, status dots or other badges.

## Column frame

The columns group stays mounted whether the sidebar is open or collapsed: only
the sidebar's own panel and its divider leave, keyed so React matches the
conversation's panel by key and keeps its component. Swapping the whole group
for a bare div unmounted the main subtree on every toggle, so the conversation
rebuilt itself — its height flashed and the reader's place came back from the
per-entry bookmark instead of staying put (user report 2026-10-20).

Collapsed, the group holds one panel and that panel takes the whole group: the
library reads a stored layout only when its key count matches the panels
present, so the stored two-panel split cannot leave a gap. Only user-driven
commits are persisted, under each frame's own layout id, so the collapse itself
writes nothing.

The conversation's own side of the same report — a width change re-wraps every
row, so the reader's pixel offset no longer points at their row — belongs to
[sticky scroll](../timeline-view/sticky-scroll.meiva.okf.md).

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
