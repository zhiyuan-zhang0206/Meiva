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

One more suite drives the same stubbed page for a chrome measurement rather than a
shell invariant: `test_work_block_chrome.py` opens `stubbed_home.work_block_items()`
— a primary human message plus one work block of six expanded children — and
asserts that the primary human message's timestamp sits inside every ancestor that
clips it (`overflow` other than visible, or `content-visibility` paint containment)
within 1px, while the work block's child rows mount neither below-card timestamps
nor copy buttons. A primary `.timeline-item` pads its bottom by 1rem (16px) and
hands that height back with a negative margin, so its hover chrome band sits
unclipped inside row containment. The same file re-asserts the Copy scope —
thinking, tool calls and tool output carry no copy control in the real DOM, a human
message does — since the jsdom suite can only see the component tree. Both tests
stub `/api/**` and the event stream even when `MEIVA_MOBILE_TEST_BASE_URL` points
the suite at a deployed bundle: they measure that fixture, not the deployment's own
data.

Suite entry points and CI integration: [[tests/docs/tests.meiva.okf.md]].

## The bottom rules' contracts (user rulings 2026-10-29, 2026-10-31 & 2026-11-01)

`test_bottom_rules_are_independent`, `test_bottom_rule_gap_is_halved` and
`test_shell_icon_size_is_uniform` measure the strip each home column ends with.
The sidebar footer's rule and the composer's rule are independent and never
aligned by design, not even at rest (user ruling 2026-11-01, replacing the
2026-10-29 shared-line ruling): the composer owns its band (`pt-3 pb-6` around a
36px control row, 73px at rest) and the sidebar footer owns a compact one
(`pt-2 pb-3` around its 28px icon row, 49px); no primitive is shared between
them. The engine test asserts the two rules are more than 1 CSS px apart at rest,
then fills a 10-line draft and asserts the footer's rule stays where it was. The
composer published its height as `--composer-band-height` and the footer took it
as a `min-height` for a day — a divider that tracked the composer's line read as
an apparatus of its own (user report 2026-10-29); the grown-state assertion is
the regression check for that coupling.

Tolerance is 1 CSS px, stated at every assertion. Both rules live in one CSS px
space, but a panel's width is a percentage of the viewport (230.234px at 1440), so
a box edge can land on a half pixel and round to a different device pixel per
column; one CSS px is still the smallest separation two *distinct* painted lines
can have, so device rounding cannot make two separate rules read as one.

The gap above the rule is one measurement made of two halves — the timeline
column's `pb-6` (24px) and the composer's `pt-0.5` (2px), 26px together — asserted
at 390, 768 and 1440 because neither class is breakpoint-dependent; a breakpoint
that grows its own padding would fail here instead of drifting silently.

## A streaming turn leaves the bottom rule still (task #5837)

`test_a_streaming_turn_leaves_the_bottom_rule_still` and
`test_a_grown_composer_holds_the_sidebar_rule_while_the_turn_streams` drive the
turn themselves: `open_page(..., scripted_sse=True)` installs a driver-fed
EventSource, and the suite emits `timeline_snapshot`, then `chat_start` plus 24
`chat_delta` chunks through `window.__meivaSse.emit`, asserting each event
reached exactly one handler. "After 24 chunks" is therefore a fact about the
events fed — no sleep, no animation-frame count, and no waiting on a real turn.
Two cases; only the second can fail:

- Both rules are measured at rest and after the chunks: the 0.5 s `llm_keepalive`
  beats re-render this surface twice a second, and the floor must not move.
- A 10-line draft has already grown the composer band, and the sidebar rule must
  hold its resting y through the draft and through every chunk while the band
  keeps the grown height. This is the reverse control for the 2026-10-29
  coupling: restore `min-height: var(--composer-band-height)` on the sidebar
  footer and this case fails, the footer 198px from its resting y (843 → 645 at
  1440×900), while the other cases stay green.

Neither case discriminates a stream-only change in the band's own height: the
stream makes none (the stop control sits in the same 73px band).

Icon size: every SVG inside the four control rows (the header bar, the sidebar's
title row, the sidebar footer, the composer's band) is `ICON_SIZE_CLASS` (16px).
A control the breakpoint hides has no box and is skipped; glyphs inside content
(the pending strip's clock, the send button's filled stop square) keep their own
optical size and are out of scope.
