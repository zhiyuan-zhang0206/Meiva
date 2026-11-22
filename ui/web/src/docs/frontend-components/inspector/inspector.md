# Inspector Panel

`components/inspector/inspector-panel.tsx` — the single-agent counterpart of the
sidebar's cluster-wide Status popover. Only the selected agent is read, only while the
panel is open; current state and window statistics load and fail
independently ([reads and refresh](queries-and-refresh.md)),
and each section's own rendering facts live in
[section details](section-details/section-details.md),
except the Activity section, which has its own node
([Activity section](activity.md)).

**Section order.** Open run timeline, persistent shells, schedules, liveness,
configuration, context, and activity appear in that order.

The Context section is shown only for a Meiva harness, as reported by the shared
roster or the already-selected detail cache when the agent is outside the roster.
This subscription makes no additional detail request. Its token totals and thresholds describe the native model window and
cannot represent an external CLI session. Unknown harness metadata also keeps
this section hidden.

**One spinner per pending read, no premature section headers or metric card skeletons.** Persistent
metric sections (`Liveness` and `Activity`) stay mounted at their ordered keys
across initial loads, agent switches and window changes. While a section's read is
pending with no matching snapshot, `Section`
(`components/inspector/inspector-section.tsx`) sets `aria-busy` and
`aria-label="{Title} loading"`, omits its section header (`Liveness` / `Activity`
and header controls) so titles do not appear before their data arrives, and
renders a single centered `Spinner` (`hideSpinner` suppresses a duplicate second
spinner in `Activity` while `Liveness` is also cold-loading; `Metric` only renders
resolved string values, user rulings 2026-11-09). Conditional and sparse sections —
`Persistent shells`, `Schedules`, `Configuration`, and `Context` — render nothing
while pending or empty rather than reserving speculative placeholder rows that
collapse after fetch.

**Single column chrome owner.** `SharedContextPanel` owns the right column's
outer chrome at every breakpoint — the desktop `document-bar` with the
`ScanSearch` toggle, the mobile full-screen sheet with its `Back to messages` +
`ScanSearch` header, and the pinned bottom `question-region`
([shared user context](../../frontend-state/shared-context/shared-context.md)
and [Inspector mode](../../frontend-state/shared-context/inspector-mode/inspector-mode.md)).
`InspectorPanel` therefore renders only its `ScrollArea` body inside `<aside>`
with no standalone header, no `embedded` prop, no inline question reply duplicate
and no second mobile backdrop. Escape deliberately does not close the panel (user
ruling 2026-08-24), and clicking outside the desktop panel does not close it
either.

**Rendering while closed.** Closing renders nothing while all hooks still run
(rules-of-hooks) with the queries disabled, so a closed panel cannot produce
inspect traffic. Closing aborts the in-flight reads.

**Agent switch.** The panel stays mounted across agent switches (the page resets
its error boundary via `resetKey`, not `key=`), so each switch also restores the
scroll-to-top reset the former remount gave a new selection.

Parent: [Frontend Components](../frontend-components.md).
