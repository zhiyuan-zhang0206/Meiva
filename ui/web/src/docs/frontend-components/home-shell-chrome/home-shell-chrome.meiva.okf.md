---
type: doc
title: Home shell chrome
description: The home columns' title bars, bottom rules, icon sizing, edge-pinned controls, and resizable column frame.
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
same line as its neighbours. A bar stays at its column's top while the column's
content scrolls beneath it: the right panel's bar sits above its reading area
rather than inside it, so a scroll never carries it away (user report
2026-10-29). Both variants are `relative`: the rule is the bar's own
`after` element, painted against the bar and not against the section around it.

Every home column title bar is BAR_HEIGHT_PX tall and paints BAR_DIVIDER_CLASS's inset rule, so the Agent Tree, Timeline and right-panel dividers sit on one line; the right panel's document bar takes the wrapping variant (44px minimum, growing only when its content cannot fit one line) and its notice header takes the fixed variant — neither draws a border on its section.

The conversation title names the agent — `Agent #<id> · <label>` — in every mode: Inspector
changes what the right column shows, so naming the bar after the mode would hide the agent
the reader is still in (user 2026-10-21). The mode is visible in its pressed toggle and in
the column itself. The bar no longer appends the harness display name (also 2026-10-21):
which harness runs the agent is state the reader checks when inspecting it, not a
label read on every glance, so it reads in the Inspector's Liveness section beside the
model
([[ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md|shared context]]).

## The bottom rules

The sidebar footer and the composer each end their column with a rule of their
own; the two are independent and never aligned by design. Their bands, their
owners and the rulings behind them are in
[[ui/web/src/docs/frontend-components/home-shell-chrome/bottom-rules.meiva.okf.md|Bottom rules]].

Above the composer's rule, the gap between the last message row and the line is split
between the list (`pb-6`, 24px) and the composer (`pt-0.5`, 2px) — 26px in all,
giving the final agent message or Live Activity Window clear breathing room
above the input bar (user ruling 2026-10-31). The list's share reaches the rule
only through the log's trailing `<div>`: without it the last row overflows the
log and the gap collapses to the composer's share alone, so the element stays
although its ref is never read.

## Icon size

One glyph size for the shell's icon-only controls: `ICON_SIZE_CLASS` (16px, the
composer's upload glyph — the reference the user named on 2026-10-29). The four
columns' control rows take it — the header bar, the sidebar's title row, the
sidebar footer and the composer's control row — so a row never mixes two glyph
sizes. The right panel's toggle and the phone's menu button used to be drawn a
size larger than their neighbours; they now take the same constant, and the
toggle's `iconClass` prop is gone with it (one size, one source).

Glyphs that are not controls keep their own optical size: an inline mark inside
a text row (the pending strip's clock, the send button's filled stop square) is
an annotation, and 16px would break the line it sits in.

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

The restore control also carries the unresolved-alert count (`alertCount`, a count bubble at its top-right corner, nothing at zero) and names it in its label: the rail is the one surface that disappears when collapsed, so the count that leads the sidebar's icon row would otherwise vanish with it (user 2026-10-21). The phone's menu button, which opens the same sidebar as a drawer, carries the same bubble, because a closed drawer hides the row for the same reason. It is the only signal a closed sidebar shows — no agent list, status dots or other badges.

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

The home frame tests run the local resizable wrappers and real `useDefaultLayout`
storage path. Only the third-party measured layout primitives are substituted;
scripted commit events preserve `isUserInteraction` and the independent layout
IDs. They prove forwarding and persistence, not browser drag geometry.
