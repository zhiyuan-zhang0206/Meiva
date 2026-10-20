---
type: doc
title: Timeline card chrome
description: The hover-revealed footer of a message card — the item timestamp and Copy, its placement in the box's bottom-right corner, and the hover, focus and touch rules.
tags:
- frontend
---

# Timeline card chrome

`components/timeline/card.tsx` — `MessageCard`. A card is the tinted box (left
accent, clickable header, collapsible body) plus a **footer** holding the item
timestamp (`components/timeline/timestamp.tsx`, `ItemTimestamp`) and, for an
expanded, settled body, Copy; either half can stand alone, and a card with
neither mounts no footer and does not join the hover group.

The footer **floats in the box's own bottom-right corner** and takes no layout
height. It used to take a line under the box, and only the kinds that stamp a
timestamp or offer Copy got that line — so the gap between two cards depended on
which kinds they were (12px between two plain cards, 28px between two stamped
ones; user report 2026-10-21). Floating makes every gap the row spacing, 12px. It
stays inside the box because `.timeline-item` carries `content-visibility: auto`,
whose paint containment clips anything drawn past the row box; the price is that
the revealed chrome sits over the body's bottom-right corner, which the hover
gate makes transient (it is only ever painted while the pointer is on the card or
focus is inside it).

The stamp carries no brackets (`2026-05-06 16:56:29`, not `[2026-05-06 …]`) and
renders at `text-[9px]`; Copy is a `size-3` icon at `p-0.5`. Both are chrome, not
content, so they stay small enough to read as a footer aside.

## Reveal rules

| State | Footer |
| --- | --- |
| Pointer over the card (`group-hover/msgcard`) | visible, interactive |
| Focus anywhere inside the card (`group-focus-within/msgcard`) | visible, interactive |
| Neither | `opacity-0` and `pointer-events-none` — not painted, not clickable, and still reachable by keyboard (focusing Copy reveals it) |
| Coarse pointer (no hover exists) | always visible, always interactive |

`opacity: 0` alone still hit-tests, so the hidden state gates `pointer-events`
too. The hidden chrome deliberately stays in the accessibility tree: a screen
reader does not hover, and removing the footer from it would take Copy away from
every non-pointer user. A touch device has no reveal gesture at all, so there the
footer is simply visible.

```meiva-contract
id = "card-actions-overlay"
kind = "invariant"
statement = "A card's timestamp and Copy float in the card box's bottom-right corner and take no layout height, so every pair of cards is the same row spacing apart: the chrome is invisible and click-through until the pointer hovers the card or focus enters it, and a coarse pointer, which cannot hover, keeps it visible."
code = ["ui/web/src/components/timeline/card.tsx:MessageCard"]
tests = ["ui/web/src/components/timeline/timeline.test.tsx::card hover footer"]
```

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
