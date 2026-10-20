---
type: doc
title: Timeline card chrome
description: The hover-revealed Copy overlay on an expanded message card — pointer gating, hit-testing, and why touch hides it entirely.
tags:
- frontend
---

# Timeline card chrome

`components/timeline/card.tsx` — `MessageCard`. The copy pill is an
absolutely positioned overlay pinned to the card's bottom-right corner, not a
sibling that pushes layout. A null `actions` prop (collapsed, partial or
not-yet-eligible rows) mounts nothing at all.

The pill is fine-pointer chrome. It reveals on `group-hover/msgcard` or
`focus-within` — a NAMED group, so hovering the card never triggers the
unrelated inner hover affordances (the code-block copy icon, markdown blocks) —
and stays `pointer-events-none` while invisible, because `opacity: 0` alone
still hit-tests: a tap in that corner would silently press an unseen button.

A coarse-pointer device gets no action. It has no real `:hover` to gate on,
and pinning the pill visible parked an icon button over the last lines of
every expanded card (user report 2026-10-17). A long press is not a substitute
reveal: that gesture already belongs to the browser's text selection. Touch
therefore copies by selecting text.

Copy is the pill's only action. Forking starts from an agent row's context menu
in the sidebar (`components/agents/agent-row.tsx`), not from a message card.

```meiva-contract
id = "card-actions-overlay"
kind = "invariant"
statement = "Copy is fine-pointer chrome on an expanded card: the pill reveals on hover or focus-within while staying pointer-events-none, and a coarse-pointer device hides it entirely rather than pinning it visible — long press already belongs to the browser's own text selection."
code = ["ui/web/src/components/timeline/card.tsx:MessageCard"]
tests = ["ui/web/src/components/timeline/card.test.tsx::MessageCard::actions prop → overlay renders, pinned inside the frame (relative/absolute), and opts the frame into group/msgcard"]
```

Parent: [[ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md|Timeline View]].
