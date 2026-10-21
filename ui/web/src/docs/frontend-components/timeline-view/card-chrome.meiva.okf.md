---
type: doc
title: Timeline card chrome
description: The hover-revealed chrome of a timeline block — the item timestamp and Copy, its band in the gap below the block, and the hover, focus, touch and expansion rules.
tags:
- frontend
---

# Timeline card chrome

`components/timeline/card.tsx` — `MessageCard` — draws the block: the tinted box
(left accent, clickable header, collapsible body). It carries no chrome of its
own. `components/timeline/row.tsx` — `TimelineRow` — owns the band under the
block that holds the item timestamp (`components/timeline/timestamp.tsx`,
`ItemTimestamp`) and Copy; either half can stand alone, and a row with neither
mounts no band and does not join the hover group.

The band **sits in the gap below the block, outside the card box** (user
2026-10-21), so the time reads as a separator between two messages instead of
eating the block's own bottom padding. `.timeline-item` owns that gap: it pads
its bottom by the same 12px the row list separates rows by, and hands the height
back as a negative margin — block-to-block spacing is unchanged and the band is
drawn inside the row box, which it must be, because `.timeline-item` carries
`content-visibility: auto` whose paint containment clips anything drawn past it.
A reserved line existed once and made the gap depend on which kinds stamped a
time (12px between two plain cards, 28px between two stamped ones); a band in the
existing gap costs no layout height at rest.

The stamp **rides an expanded block only**: a collapsed row is a one-line header,
and a time under it read as chrome for a message that is not showing (user
2026-10-21). Copy has always been expanded-only — it attaches to an expanded,
settled body. The block's own vertical insets are symmetric: the header's 6px
top padding and the body's 4px bottom padding both measure 7px from the text to
the box edge, so a block does not look bottom-heavy.

The stamp carries no brackets (`2026-05-06 16:56:29`, not `[2026-05-06 …]`) and
renders at `text-[9px]`; Copy is a `size-2.5` icon with no padding, so the control
fits the 12px band instead of overlapping the card edge it now sits below, and
reads no taller than the stamp beside it (user 2026-10-22 — at `size-3` the glyph
was visibly bigger than the time). Its hit area is an invisible `after:-inset-1`
box, not a painted one.

## Reveal rules

| State | Band |
| --- | --- |
| Pointer over the block (`group-hover/msgcard`) | visible, interactive |
| Focus anywhere inside the block (`group-focus-within/msgcard`) | visible, interactive |
| Neither | `opacity-0` and `pointer-events-none` — not painted, not clickable, and still reachable by keyboard (focusing Copy reveals it) |
| Touched on a coarse pointer (`TimelineRow` holds the band up for 6 s) | visible, interactive; hidden again after the window |

`opacity: 0` alone still hit-tests, so the hidden state gates `pointer-events`
too. The hidden chrome deliberately stays in the accessibility tree: a screen
reader does not hover, and removing it would take Copy away from every non-pointer
user. A touch device has no hover, so touching a block is its reveal gesture: the
band comes up for `TOUCH_REVEAL_MS` and drops again, instead of standing painted
under every message for good (user 2026-10-22 — the always-visible band was the
bug). A mouse press does not take that path.

```meiva-contract
id = "card-actions-overlay"
kind = "invariant"
statement = "A timeline block's timestamp and Copy stand in the 12px gap below the block, outside the card box and inside the row's own padding, so block-to-block spacing is unchanged: the stamp appears only on an expanded block, the band is invisible and click-through until the pointer hovers the block, focus enters it, or a touch reveals it for a few seconds."
code = ["ui/web/src/components/timeline/row.tsx:TimelineRow", "ui/web/src/components/timeline/card.tsx:CARD_FOOTER_CLS"]
tests = ["ui/web/src/components/timeline/timeline.test.tsx::card hover footer"]
```

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
