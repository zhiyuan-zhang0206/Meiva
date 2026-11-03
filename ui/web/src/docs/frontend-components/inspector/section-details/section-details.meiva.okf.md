---
type: doc
title: Inspector section details
description: What each Inspector section renders — shells, pages, the config-overlay diff, plugin widgets and the no-fabrication rule; the Activity and Liveness sections have their own nodes.
tags: [frontend]
---

# Inspector section details

Rendering facts of the panel's sections; the shell (layout, order, loading
contract) is in [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md|Inspector Panel]]
and the reads behind them in
[[ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md|reads and refresh]].

**Open run timeline row.** The top entry is a link to `/insights/run/{agentId}`
that wears the same row chrome as the Page section's rows — the rounded
`bg-sidebar-accent/40` (`dark:bg-[#2a2926]`) block, a leading external-link icon that brightens on
hover, `11.5px` monospace values in anti-glare `#f0ede7` on dark surfaces, and the hover highlight — so the panel's clickable rows and `Metric` cells read as one
family. The panel's amber stale dot renders at its end — it marks
"live refresh is failing" only when a snapshot is already displayed
(stale-while-error); a cold failure gets the full error message in the body
instead, so the dot never replaces content. The fleet task link (`fleetTaskHref`)
is a typed literal route so `next/link` infers it without a cast.

**Activity** carries the cost, TPS and duration cells in its own node: [[ui/web/src/docs/frontend-components/inspector/activity.meiva.okf.md|Activity section]].

**Context** is the selected agent's context-window occupancy: a wide gauge
(`ContextMeter`, fixed at its comfortable `w-32` track width outside the
full-width Inspector slot, ticks at the soft and hard thresholds) with the
header badge showing occupancy as a percentage, then one row of two
side-by-side cells — used / window, and soft / hard limit in tokens (user ruling
2026-10-18: one row, not two stacked blocks). Clicking the gauge opens the
shared floating breakdown above it. It reads the timeline store's token slice,
the values `useTokenUsage` keeps for the selected agent, so the panel never
disagrees with the timeline about whose window it is showing. The section
renders nothing before the first LLM call (all zeros). Context occupancy lives
exclusively in the Inspector and on `/insights/run/{agentId}` — the composer
carries no context meter (user ruling 2026-11-01).

**Page** lists the agent's open pages as external links (title or name plus the
URL), hidden entirely when none is open; the read is `useAgentPages`.

**Persistent shells.** The badge shows the shell count, or `?` when observation
is unavailable — an unavailable observation must not render as an empty set. A
row links to its full-screen monitor page `/shell/{agentId}/{shellId}` and shows
only the ticking runtime (launch → now, computed per second from `created_at`;
falling back to the probe-time `uptime_seconds` snapshot when `created_at` is
missing). The created/TTL detail deliberately lives on the monitor page's title
bar, not here (user corrections 2026-08-28). A row whose agent or shell id is
not a valid finite integer renders as plain text, so the panel never navigates
to `/shell/NaN/NaN`.

**Configuration overlay.** Rows render every overlay entry's value — strings
pass through, everything else is JSON-stringified. `llm_model` is skipped: the
model is the agent's state, and it reads in Liveness instead (user 2026-10-21). Skill-list keys
(`skills_to_inject_into_system_prompt`, `skills_to_expand_at_start`) are stored
in the runtime's underscore Python projection and render in the canonical dash
spelling (the same rule as the `# Capabilities` index); `*` stays `*`.

**Liveness.** One merged section whose cells are birth, harness and model, the
next heartbeat, the last pause, machine and status — the cell rules, including
what a pause or a queued wake renders, are in
[[ui/web/src/docs/frontend-components/inspector/section-details/liveness.meiva.okf.md|the Liveness section]].

**Widgets.** Plugin widgets render through the shared section chrome
(`inspector-section.tsx`), which also owns `Section` loading attributes and
`Metric`'s `17px` pending breathing bar, so plugin sections and built-in ones
share one header and card primitive; the widget section itself renders only the
closed-set kinds the console knows (`InspectWidgetSection`), renders nothing
while pending or empty, and a widgets fetch failure leaves the rest of the panel
intact. Open notices are rendered once in `SharedContextPanel`'s pinned bottom
`notice-region` ([[ui/web/src/docs/frontend-state/shared-context/shared-context.meiva.okf.md|shared user context]]),
never duplicated inside the Inspector scroll body.

**No fabrication.** The panel renders the metrics, or nothing: coverage
verdicts, duration precision and retained-source notes are machinery that go to
the background log and alert episodes instead of the UI (user ruling 2026-09-17,
task #3869); unavailable metrics never become zero cost, zero throughput or a
verdict, and the fleet Alerts block is not part of the inspector (user ruling
2026-08-29). Formatting helpers are shared (`formatTokens`, `formatRelative` /
`formatAbsolute` / `formatShort` / `formatUpcoming` / `formatUptime`), so tests
assert against the shared helper's own output rather than re-deriving it. The
day-context table this section quotes is owned by
[[ui/web/src/lib/format/docs/time.meiva.okf.md|Timestamp display]].

Parent: [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md]].
