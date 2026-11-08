---
type: doc
title: Loading states
description: The console's one loading indicator — a bare turning ring, never a loading sentence and never a skeleton — and where its size and accessible name come from.
tags:
- frontend
---

# Loading states

**A loading state is the turning ring and nothing else** (user ruling 2026-11-08).
`components/ui/spinner.tsx` is the console's one loading indicator: `Spinner`
renders a spinning `Loader2`, takes an optional `label` for a screen reader — an
attribute, never painted text — and breathes instead of spinning for a reader who
asked for reduced motion. Every data-loading surface uses it: a route
`loading.tsx`, a pending read inside a page, a per-row fetch, a button whose
action is in flight.

**No loading sentence.** Nothing writes "Loading…" beside the ring. The i18n
string a read would have shown becomes the spinner's `label`, so the state still
has an accessible name while the screen shows only motion.

**No skeleton.** Nothing paints a placeholder shaped like the content it stands
in for: not a bar where a value will land, not a grey block standing in for a
chart, not a ghost of the page's structure. A skeleton that does not match the
real structure reads as broken layout, and one that does match is only a slower
thing to look at. Where the resolved content has a height, the loading frame keeps
it — the run-timeline panel at the chart's height, the inspector's `17px` value
line — so the swap costs no jump.

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
