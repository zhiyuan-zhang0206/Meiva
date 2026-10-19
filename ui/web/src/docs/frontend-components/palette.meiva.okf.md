---
type: doc
title: Colour tokens and the dark palette
description: How the console's surfaces are coloured — the light ramp, the warm-graphite dark ladder, and the two component rules the palette cannot replace.
tags:
- frontend
---

# Colour tokens and the dark palette

`ui/web/src/app/globals.css` owns both blocks; the themable properties are declared
in `base/packages/plugins/surface/ui_contributions.py:THEME_TOKENS`, a test keeps
the two lists equal, and a skin (plugin theme pack) may repaint any of them.

## The light block

The neutral shadcn ramp. Nothing special: surfaces differ by small lightness steps
and borders carry the separation.

## The dark block: warm graphite

User pick 2026-10-19. One near-neutral warm family (OKLCH hue 75, chroma 0.005) and
a four-step elevation ladder. The **canvas is the darkest surface** and the sidebar
sits just above it; cards and popovers rise above the canvas:

| Surface | Token | Value |
|---|---|---|
| canvas | `--background` | `#1e1d1b` |
| sidebar | `--sidebar` | `#232220` |
| card | `--card` | `#2b2a27` |
| popover | `--popover` | `#35332f` |

The order matters. The conversation is the field the eye reads, so it is the
quietest, deepest surface; chrome recedes into it rather than floating above it.
The canvas-to-sidebar step stays tiny on purpose (1.06:1): a wider gap reads as
two competing surfaces, which is what the first draft of this ladder did (user
report 2026-10-19). Cards and popovers still rise, so depth comes from the ladder
and a dark surface never needs a 2px line to separate two panes.

The block it replaced failed on structure rather than on hue: the card sat *darker*
than the canvas (OKLCH 0.205 vs 0.25), the sidebar, card and popover were the same
value, and every surface was pure gray with a chroma of zero. Those three fixed
together — a ladder, one temperature, and a `--muted-foreground` that clears 7:1 on
the canvas — are what the palette is.

Measured from computed styles on the rendered page (Chromium, not from the token
table): body text 15.6:1 on the canvas, muted text 8.0:1 on the canvas and 6.0:1 on
a popover, popover text 11.7:1, sidebar text 14.8:1; ladder steps canvas-to-card
1.17:1, canvas-to-popover 1.34:1, canvas-to-sidebar 1.06:1.

## Two component rules that ride along

Neither is cosmetic, and neither is fixed by choosing colours:

- neutral blocks inside the timeline (`ui/web/src/lib/timeline/timeline-colors.ts`,
  the `gray` family) paint with `--muted` / `--muted-foreground` on dark instead of
  Tailwind's cool gray ramp, because a cool patch on a warm canvas reads as a
  rendering bug;
- timestamps use `text-muted-foreground/80`, not `/60` — at 10px, `/60` measured
  3.3–3.6:1, below the 4.5:1 floor for small text.

## What the app cannot do yet

There is no in-app light/dark toggle: `next-themes` follows the system, so the two
reachable states are OS-dark and OS-light. A per-user override would be a new
setting, not a palette change.
