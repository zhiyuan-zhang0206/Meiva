---
type: doc
title: Agent Sidebar
description: The Agent Tree column — one header row of icons, the list body, display settings and creation behind popovers, and the quiet defaults it keeps.
tags:
- frontend
---

# Agent Sidebar

The rail is a list with one header row above it. That row holds search, the
awaiting-reply badge (opt-in), the display controls (sliders, not a gear — the footer's Control entry already is one), create, and collapse;
the body below it is only the tree or flat list, so a 230px column spends no
height on controls.

## Footer icon row

The row sits in the sidebar's own compact band (rule and `py-1` around the 28px
icon row — 37px). Its rule is independent of the composer's and never aligned with
it by design; it neither shares the composer's band nor follows the composer's
height (user ruling 2026-11-01;
[[ui/web/src/docs/frontend-components/home-shell-chrome/bottom-rules.meiva.okf.md|Bottom rules]]).

Below the list, one row of shortcuts in a fixed order: the unresolved-alert bell,
Fleet, Insights, Control, then whatever a plugin contributes (`PluginNavIcons` renders
nothing when no plugin declares an entry). The bell leads the
row because a standing cluster state outranks navigation (user 2026-10-21), it
links to the alert section, and it renders only while the count is non-zero. The
row leaves with the rail when it collapses; the count then rides `HeaderBar`'s
restore button (desktop) or its menu button (phone drawer) instead of vanishing
([[ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md|Home Shell Chrome]]).

## Status popover

The chart icon at the head of the footer row opens the Status popover: a quick
status of the whole cluster, not a statistics report. Mirroring the Inspector
panel's split between point-in-time state and time-aggregated activity, the
popover separates its unwindowed state above from its windowed `Usage` section
below:

- **Point-in-time section** (above the rule, never governed by a window picker):
  `Live agents` and any plugin-declared status cards (`contributions.ui.stats`,
  such as provider balances and subscription quota windows).
- **Windowed `Usage` section** (below the rule, headed by `Usage` and its own
  `WindowSelect` offering `5m` / `1h` / `6h` / `24h` / `3d` / `7d` / `30d`): a
  2-column grid of `Tokens`, `Cache hit`, `Cost (API)`, `Cost (subscription)`,
  `Warnings / errors`, and metered `OpenAI API` and `Anthropic API` spend slices.
  The two cost cards are the same disjoint split the Inspector shows, by the
  billing mode of the harness that ran each call: `Cost (API)` is the fee the
  account is billed (subscription harnesses excluded, so monthly API credit
  consumption stays distinct from subscription quotas) and `Cost (subscription)`
  the list value those harnesses covered — consumed quota, not money owed. Both
  cards always render, `$0.00` included (user ruling 2026-11-08): the pair is a
  statement about two billing modes, and dropping a zero half would make it read
  as the single total it replaced. The Inspector keeps the opposite rule (a zero
  block disappears) because there each block is a claim about one agent's spend.

Keeping the panel behind the footer trigger means the closed sidebar spends no
height on numbers a reader checks occasionally (the old inline bar occupied a
row permanently).

## Display settings popover

The sliders button opens view (tree / flat, `display.*` user setting), the sort key and
its direction, the extra status markers toggle and the terminated archive. Those four
were a permanent toolbar row for settings a user picks once. Clicking the
active sort key flips the direction; the arrow shows which way it points. The
awaiting count is not a setting and not here: it is a live signal, so it sits
in the header as a badge.

## Create popover

The + opens the spawn form and mounts `SpawnButton` only while the popover is
open, so the machine / model / effort / harness pickers never cost the closed
sidebar a row. Desktop fine-pointer viewports render the multi-column table;
narrow viewports and coarse-pointer tablets (`useCoarsePointer()`) render the
stacked form (`layout="stack"`) so touch rows stay finger-sized and surface
unavailability reasons inline without relying on hover tooltips. The form itself is unchanged — placement, pricing and harness
gating stay with [[ui/web/src/docs/frontend-components/creation-availability/creation-availability.meiva.okf.md|Creation Availability]].
An empty roster replaces the list with one explanatory line and a full-width
create button, because a bare + does not explain itself to someone who has
never spawned an agent.

The mobile drawer carries the same settings and create controls: the body is
shared with the rail, so a control that lives only in the rail's header would
silently disappear on a phone.

## Quiet defaults and state

Collapsed hides the column entirely; the title bar's PanelLeft button restores
it. The list is stably sorted by agent ID descending and status or activity
changes never re-sort it. Each row renders as a compact inset Crisp Card pill
(`.agent-row-item` / `.agent-row-pill`) with softened charcoal active text
(`#3B3B3B` light / `#E2E4E8` dark at `font-weight: 450`) and readable mid-gray
idle text (`#525252` light / `#A8ABB0` dark). A running agent's title sweeps
with a lighter monochrome Silver Glint highlight (`agent-title-sweep`) over its
normal row base color so running items never darken or bolden relative to idle
rows (and stays at static row emphasis under reduced motion).
Offline agents retain a gray marker. At the right edge, an unresolved structured
question shows a Sunset Orange (`#ED8755`) circled question mark, and an ordinary
response-required notice shows a Sunset Orange (`#ED8755`) dot. The question
takes precedence when both are pending, independently of the working title.
Unread publications and FYI notices do not need a reply. Additional lifecycle
markers and the header's aggregate reply count remain opt-in through
`display.show_agent_status` and `notification.awaiting_reply`, respectively.
Rows keep timestamps in the hover card. Labels truncate with a single-line
ellipsis (`truncate`) so long names and deep
tree indents never wrap or vary row height. On fine pointers (`!useCoarsePointer()`),
hovering a row for 400 ms opens a read-only peek card (`agent-hover-card.tsx`) to
the right of the sidebar with the full untruncated `#ID · label`, lifecycle status,
harness, machine, the `Model` and `Effort` the agent's calls actually run with
(each on its own row, resolved from the roster: the agent's pin, else the model's
own default, else the selection a subscription harness settled at launch — a
sub-catalog model id a harness brought keeps its own name), relative `Active` and
`Spawned` timestamps, and any active lineage or anomaly signals; moving to another row within a 300 ms warm-up window
switches the card immediately (0 ms), and leaving closes it at once (coarse
pointers suppress synthetic hover tooltips). Rows can be renamed inline either by
double-clicking the label or by choosing `Rename` from the context menu (long-press
on touch, right-click on desktop).
Status arrives over SSE. Every home entry expands the rail once: the one-time
reset lives in `HomeShell` (which survives layout-frame switches while the
sidebar remounts) and waits for the persisted settings, so a cold load with
`collapsed=true` still expands.
