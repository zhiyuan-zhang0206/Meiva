---
type: doc
title: Visual shell layout invariants
description: The real-engine half of the shell layout defense — the Playwright suite's tiers, scroll-surface reveal, on-screen-keyboard and frosted-pane contracts over the stubbed Home page.
tags:
- evaluation
- quality-assurance
---

# Visual shell layout invariants

The real-engine half of the two-layer layout defense (task #1024 R4): every named
layout invariant in `ui/web/src/lib/layout.ts` gets a Playwright assertion at the
320 / 390 / 768 tiers; the jsdom half lives in `page.test.tsx` + `layout.test.ts`.

The suite drives the stubbed Home page (`stubbed_home.py`: a fake EventSource,
`/api/**` stubs, shared context/open/settle helpers). A fresh browser context
cannot open the real cookie-gated SSE stream and `page.route` cannot stream to an
EventSource, so an injected fake carries the snapshot → delta → reconnect
sequence; without a growing stream every invariant would false-pass.
The shared agent stub explicitly owns the Meiva harness, matching the required
roster/detail contract. Its system prompt supplies the pinned work-detail block;
an unknown or external harness correctly hides that native prompt.
`MEIVA_MOBILE_TEST_BASE_URL` set targets that URL with real data (cookie via
`MEIVA_TEST_SESSION_COOKIE`) and still injects the fake; unset serves the
session-scoped `frontend_proc` build with stubbed `/api/**` (CI mode). Engine:
chromium — CI installs chromium only; webkit is reserved for the iOS-Chrome
popover test.

The real-engine layer asserts the named layout invariants at the 320/390/768 tiers: I1 no page-level horizontal scroll (home and fleet), I2 the timeline surface stays inside its parent, I3 the composer stays inside the viewport, I4 the Tasks toolbar never widens the fleet page, I5 inbox rows never overflow their container, I6 the page does not scroll as a whole — the min-h-0 flex chain routes scrolling into the timeline surface.

The shared ScrollArea track stays hit-testable at opacity 0 and reveals on hover or scroll without taking layout space: the track reaches full opacity while hovered and after a wheel scroll, content hover and idle hide it back, the viewport keeps its client width (no native-scrollbar reservation), and no reveal widens the document. The inspector's secondary view carries the same overlay scroll contract as the timeline.

With the on-screen keyboard up the composer stays inside the visible band: the viewport meta asks Chrome to resize content (interactive-widget=resizes-content, the Android half, which Safari ignores) and the shell pins <html> to the shrunken visualViewport height (the iOS half). A shortfall below the 150px keyboard threshold and a pinch-zoom leave the shell unpinned, and closing the keyboard restores the layout height.

The frosted pinned-header pane fades between stuck and unstuck, so its ::before background is read only after one full live transition-duration plus a bounded stability poll — a mid-fade read was the #3464 CI flake — and a genuine regression still fails at the poll deadline instead of hanging. The sealed pane stays behind its label (z -10, blur 12px, tint, separator) and hover changes nothing but the tint.

Suite entry points and CI integration: [[tests/docs/tests.meiva.okf.md]].

## The bottom band's two contracts (user ruling 2026-10-29)

`test_bottom_rule_is_one_line`, `test_bottom_rule_gap_is_halved` and
`test_shell_icon_size_is_uniform` measure the strip every home column ends with.
Both columns are bottom-anchored, so the sidebar footer's rule and the composer's
rule can only be one line if both bands are composed the same way — the code
shares `BOTTOM_BAND_CLASS` / `BOTTOM_BAND_ROW_CLASS` with the composer, and the
composer publishes its content-sized height as `--composer-band-height` so the
footer follows a growing draft. The engine test therefore re-measures the pair
after filling a 10-line draft: a rule that parts as soon as the reader types
would be a regression the resting-state assertion cannot see.

Tolerance is 1 CSS px, stated at every assertion. Both rules live in one CSS px
space, but a panel's width is a percentage of the viewport (230.234px at 1440), so
a box edge can land on a half pixel and round to a different device pixel per
column; one CSS px is still the smallest separation two *distinct* painted lines
can have, so the 16px drift this rule replaced fails while device rounding passes.

The gap above the rule is one measurement made of two halves — the timeline
column's `pb-1.5` and the composer's `pt-1`, 10px together — asserted at 390, 768
and 1440 because neither class is breakpoint-dependent; a breakpoint that grows
its own padding would fail here instead of drifting silently.

Icon size: every SVG inside the four control rows (the header bar, the sidebar's
title row, the sidebar footer, the composer's band) is `ICON_SIZE_CLASS` (16px).
A control the breakpoint hides has no box and is skipped; glyphs inside content
(the pending strip's clock, the send button's filled stop square) keep their own
optical size and are out of scope.
