---
type: doc
title: Inspector Panel
description: The right-side panel body for the selected agent — one ordered section list, in-place metric loading with a bare spinner and no placeholder bar, and the render-nothing-while-closed boundary.
tags: [frontend]
---

# Inspector Panel

`components/inspector/inspector-panel.tsx` — the single-agent counterpart of the
sidebar's cluster-wide Status popover. Only the selected agent is read, only while the
panel is open; current state, window statistics and plugin widgets load and fail
independently ([[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]]),
and each section's own rendering facts live in
[[ui/web/src/docs/frontend-components/inspector/section-details/section-details.meiva.okf.md|section details]],
except the Activity section, which has its own node
([[ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md|Activity section]]).

**One ordered list, no filter.** The body is a single list of sections
(`INSPECT_SECTION_ORDER` in `lib/inspector/inspector-widgets.ts`): the Open run
timeline entry (100) first, then page (200), then the runtime sections in their
established order — shells (300), liveness (400), configuration overlay (500) —
then the Context section (600), and the Activity section (900) last (user ruling
2026-10-18). Plugin widgets carry their own `order` and slot between those keys;
equal orders stack built-in sections first, then widgets by (plugin, id), so the
sort is deterministic regardless of registration order and DOM order is visual
order.

The Context section is shown only for a Meiva harness, as reported by the shared
roster or the already-selected detail cache when the agent is outside the roster.
This subscription makes no additional detail request. Its token totals and thresholds describe the native model window and
cannot represent an external CLI session. Unknown harness metadata also keeps
this section hidden.

**In-place loading, one spinner per cell, never a placeholder.** Persistent
metric sections (`Liveness` and `Activity`) stay mounted at their ordered keys
across initial loads, agent switches and window changes. While a section's read is
pending with no matching snapshot, `Section`
(`components/inspector/inspector-section.tsx`) sets `aria-busy` and
`aria-label="{Title} loading"`, and each `Metric` cell renders the shared
`Spinner` inside the exact `17px` value line-box of its real card chrome: resolving
values causes zero height shift or DOM remount, and the pending state never paints
a bar shaped like the value it stands in for (user ruling 2026-11-08 — a loading
state shows the turning ring alone, with no loading sentence and no skeleton
anywhere in the console). Conditional and sparse sections — `Page`, `Persistent
shells`, `Configuration`, `Context` and plugin widgets — render nothing
while pending or empty rather than reserving speculative placeholder rows that
collapse after fetch.

**Single column chrome owner.** `SharedContextPanel` owns the right column's
outer chrome at every breakpoint — the desktop `document-bar` with the
`ScanSearch` toggle, the mobile full-screen sheet with its `Back to messages` +
`ScanSearch` header, and the pinned bottom `notice-region`
([[ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md|shared user context]]
and [[ui/web/src/docs/frontend-state/shared-context/inspector-mode/inspector-mode.meiva.okf.md|Inspector mode]]).
`InspectorPanel` therefore renders only its `ScrollArea` body inside `<aside>`
with no standalone header, no `embedded` prop, no inline notice reply duplicate
and no second mobile backdrop. Escape deliberately does not close the panel (user
ruling 2026-08-24), and clicking outside the desktop panel does not close it
either.

**Rendering while closed.** Closing renders nothing while all hooks still run
(rules-of-hooks) with the queries disabled, so a closed panel cannot produce
inspect traffic. Closing aborts the in-flight reads.

**Agent switch.** The panel stays mounted across agent switches (the page resets
its error boundary via `resetKey`, not `key=`), so each switch also restores the
scroll-to-top reset the former remount gave a new selection.

Parent: [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md]].
