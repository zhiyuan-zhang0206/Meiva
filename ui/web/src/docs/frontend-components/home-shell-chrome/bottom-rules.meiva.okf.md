---
type: doc
title: Bottom rules
description: The sidebar footer's and the composer's bottom rules — two independent bands, each owned by its own column, never aligned by design.
tags:
- frontend
---

# Bottom rules

The sidebar footer and the composer each end their column with a rule of their
own, and the two rules are independent: they are never aligned by design, not
even at rest (user ruling 2026-11-01, replacing the 2026-10-29 shared-line
ruling). No layout primitive is shared between them, and neither band's
geometry is derived from the other's.

## The composer's band

A 1px rule, 12px (`pt-3`), its 36px control row (the `size-9` upload button and
the `min-h-9` textarea), and 24px (`pb-6`) of bottom viewport clearance (user
ruling 2026-10-31) — 73px at rest, owned by
`components/conversation/composer.tsx`. It is content-sized: a multi-line
draft, the context-meter row or an upload hint grows it upward, so its rule
rises with them. The gap above this rule is described in
[[ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md|Home shell chrome]].

## The sidebar footer's band

Sized for its own 28px icon row: a 1px rule and 4px (`py-1`) above and below
the row — 37px, owned by `components/agent-sidebar/footer.tsx`. It follows no
other column's geometry: it neither takes the composer's control-row minimum
nor tracks the composer's measured height (user report 2026-10-29 removed the
day the footer took the composer's published height as its minimum).

## Verification

The jsdom tests pin each band's own classes (`composer.test.tsx`,
`footer.test.tsx`); the real-engine suite measures that the two rules are apart
at rest and that a grown composer leaves the footer's rule still
([[tests/e2e/visual/docs/layout-invariants.meiva.okf.md|Visual shell layout invariants]]).
