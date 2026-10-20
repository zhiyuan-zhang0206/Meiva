---
type: doc
title: Timeline card chrome
description: The hover-revealed footer under a message card — the item timestamp and Copy, its placement outside the card box, and the hover, focus and touch rules.
tags:
- frontend
---

# Timeline card chrome

`components/timeline/card.tsx` — `MessageCard`. A card has two parts: the tinted
box (left accent, clickable header, collapsible body) and the **footer** that
follows it. The footer is a sibling of the box, not part of it, so the box keeps
its own padding and background and the footer never covers the body. It holds
the item timestamp (`components/timeline/timestamp.tsx`, `ItemTimestamp`) and, for an expanded, settled
body, Copy; either half can stand alone, and a card with neither mounts no
footer and does not join the hover group.

The footer takes its own line under the box, right-aligned in the card's
`text-[13px]` column. It stays in the flow rather than floating over the
inter-row gap: `.timeline-item` carries `content-visibility: auto`, whose paint
containment clips anything drawn past the row box, and reserving the line also
keeps the card from jumping when the chrome appears.

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
every non-pointer user. A touch device has no reveal gesture at all, so there
the footer is simply visible — it sits outside the box, so it cannot cover the
body the way the old in-card pill did (user report 2026-10-17).

```meiva-contract
id = "card-actions-overlay"
kind = "invariant"
statement = "A card's timestamp and Copy live in a hover footer outside the card box: it is invisible and click-through until the pointer hovers the card or focus enters it, and a coarse pointer, which cannot hover, keeps it visible."
code = ["ui/web/src/components/timeline/card.tsx:MessageCard"]
tests = ["ui/web/src/components/timeline/timeline.test.tsx::card hover footer"]
```

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
