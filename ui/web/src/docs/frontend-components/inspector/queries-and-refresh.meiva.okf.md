---
type: doc
title: Inspector reads and refresh
description: Query ownership, response identity guards, refresh triggers and the per-agent retention window of the Inspector Panel.
tags: [frontend]
---

# Inspector reads and refresh

Three reads with independent loading and error state
(`lib/inspector/inspector-queries.ts`): current state (`inspectLiveQueryKey`),
the window statistics of the selected time range (`inspectWindowedQueryKey`), and
the plugin widgets (`inspectWidgetsQueryKey`). All three keys carry the agent id,
run only while the panel is open and only for the selected agent, and abort their
HTTP request when the panel closes or the selection switches away (an observer
being disabled does not by itself cancel transport).

**Response identity.** Beyond the agent-scoped keys, each response is re-checked
against the selection before it can render: current state and widgets must echo
the selected agent, statistics must also echo the requested window
(`matchesInspectWindow`). A malformed or misrouted payload is never rendered —
not even as plain text under React Query's `keepPreviousData` — so one agent's
inspector can never appear under another selection. A new window's pending state
replaces the prior window's values with in-place metric breathing bars instead of
showing the old numbers or remounting the section.

**Refresh ownership.**

- Selection: opening and every agent switch are the query's mount trigger
  (`refetchOnMount: "always"`); a hot switch-back to a retained agent must and
  does refetch the two live reads (the panel is not remounted per switch, so an
  invalidate-on-transition owns this).
- Manual refresh: only the Cost + Activity section header's ⟳ exists; it
  re-fires the windowed statistics read alone.
- Window picker: re-scopes the windowed statistics read; `All` is cumulative
  since spawn (`null`), `5m` is `0`, and `1h` / `24h` / `7d` are the backend's
  supported subset. When the backend clamps the request to its retention, the
  option label shows the applied hours (`applied_window_hours`).
- Notice events: the global notices fold invalidates only the live query without
  re-reading statistics.
- Task events (`task_created` / `task_updated` for the selected agent): refresh
  the widget read only, since task movement can change the task a widget row
  points at.
- Page events: the open-pages list (`["agent-pages", id]`) is refreshed through
  `useAgentPages`' own coalesced read.
- 60-second interval (while open) reconciles statistics; compact invalidation is
  commit-safe; a reconnect repairs the live and widget reads immediately
  (statistics reconcile on their next interval), and repeated reconnect storms
  never restart or refetch the expensive historical work.

Closing the panel aborts the browser's in-flight reads, but the gateway's
shared aggregate work keeps its own bounded admission and deadline — a cancelled
HTTP waiter does not shorten it.

**Failure rendering.** A failed refresh that still has data quietly keeps every section
without amber stale dots or notices (transient refresh failures remain silent while
TanStack Query retries in the background); a cold failure with no data replaces
the section body with an `InlineError` + retry (`Liveness` for the live read,
`Activity` for the windowed read, and an error block below `Page` when the widget
read fails without cached widgets), and a retry re-fires only that section's read —
the live failure cannot hide statistics or widgets and vice versa.

**Retention and the switch budget** (`lib/layout/switch-budget.ts`): inactive
entries persist for `INSPECTOR_RETENTION_MS` (30 minutes) per agent — both as
query `gcTime` and as the widget read's `staleTime` — so a back-switch inside the
window renders the retained snapshot immediately (no cold skeleton) and
revalidates at most the two live reads, never a third widget read; only the
selected agent's keys are ever rendered, so retention cannot leak across agents.

Parent: [[ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md]].
