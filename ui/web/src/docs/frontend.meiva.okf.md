---
type: doc
title: Frontend UI
description: Meiva's web interface — Next.js 16 + React 19 + Tailwind 4 + shadcn/ui. Browser directly connects to Gateway, with SSE push and bounded snapshot polling. Stack + pages/routes + Provider composition.
tags:
- frontend
---

# Frontend UI

Meiva's web user interface — Next.js 16 (App Router) + React 19 + Tailwind CSS 4 + shadcn/ui (Radix). Runs on port 3000, browser **directly connects** to Gateway API (`<host>:8000`, `credentials: include` with session cookie), does not go through Next.js rewrites proxy (Turbopack dev proxy buffers SSE).

## Tech stack

| Layer | Selection |
|---|---|
| Framework | Next.js 16 (App Router) |
| UI | React 19 |
| Styling | Tailwind CSS 4 |
| Component library | shadcn/ui (Radix primitives) |
| Font | Inter + Geist Mono |
| Theme | next-themes (class strategy, follow system) + explicit `color-scheme` meta/CSS; the palettes are [colours and the dark palette](frontend-components/palette.meiva.okf.md) |
| Server data | TanStack React Query |
| Real-time comms | EventSource (SSE) |

## Pages / Routes

```
/                             Chat view — sidebar + timeline + composer (homepage)
/fleet                        Fleet supervision view — relationship graph + Task graph + unified Inbox queue (InboxQueue, replaces old Decisions/Reviews dual queues)
/login                        Login page
/shell/[agentId]/[sessionId]  Terminal session (session backend proxy)
/control                      Control vertical long page — write/admin surface (observation surface split to /insights)
                              First-level section anchors: guide/config/display/plugins/mcp/skills/schedules/okf-graph (8th section, added 2026-07-24: button opens /api/okf/graph — D3 force-directed .meiva.okf.md doc graph, rebuilt from the current doc tree per request)
                              Top header: chat link + title; left two-level anchor nav (ControlNav) + right single scroll container (replaces TabBar)
                              Structure + data queries all ready on first screen (not expanded on scroll); IntersectionObserver only pauses polling after leaving viewport (enabled:visible,visibility.tsx, saves connection budget)
                              Anchor jump instant (scrollIntoView behavior:auto)
                              Section components are also bare routes: control/{guide,config,display,schedules,inventory,skills}/page.tsx
                              inventory/page.tsx exports PluginsInventory/McpInventory (shared /api/inventory query) as Plugins+MCP sections
/insights                     Observation surface (read) — Status + Ops + Alerts sections, same shell as Control (sharing ControlNav + single scroll container INSIGHTS_SCROLL_ID)
                              Visibility-bounded Status polling (15s); readiness, host roster and reported release observations. No deployment mutation or source-checkout update preflight.
                              Ops links to Grafana; Alerts is live via SSE.
                              Section components are also bare routes: insights/{status,ops}/page.tsx; insights/alerts/page.tsx redirects to the Alerts anchor
/insights/run/[agentId]       Run-level tracing view — a direct, shareable linear turn track for one agent, with prioritized event connectors and click-through turn details (time, usage, cost, model, execs, anomalies). It reads the gateway's bounded run session, offers explicit start/end and zoom windows, requests one-hour buckets up front for explicit windows of at least six hours, and switches after a turn response above 400 rows. Insights and the active inspector link here.
/control#skills               Installed skills read-only table: name/layer origin (core=repo|plugin|machine=user|untracked)/enabled/local drift (GET /api/skills, single-machine gateway local read)
```

## Core principles

- **SSE first**: timeline/token fold events locally; agent and page events trigger Query repair with no polling. Snapshot-only surfaces poll only while visible/enabled and share a page-level cadence where they have multiple consumers.
- **No optimistic write**: state changes await SSE event confirmation; lifecycle actions only flip `isPending`.
- **Single writer**: each cache/flag has exactly one writer.

## Deeper topics

- [[ui/web/src/docs/provider-composition.meiva.okf.md|Provider composition]] — root provider tree (`QueryClient`, `EventStreamProvider`, fold owner, root hosts, `VisualViewportHeightSync`)
- [[ui/web/src/docs/mobile-and-tablet-ux.meiva.okf.md|Mobile, tablet and coarse-pointer UX]] — orthogonal geometry vs. input/viewport physics contract across phones, iPads, and WebKit
- [[ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md|State management]] — three mechanisms division of labor + per-thread timeline cache + sticky controller
- [[ui/web/src/docs/frontend-data-flow/frontend-data-flow.meiva.okf.md|Data flow]] — dual SSE Provider architecture + hook directory
- [[frontend-components.meiva.okf.md|Components]] — chat view / Fleet view / settings page components
- [[ui/web/src/docs/frontend-components/loading-states.meiva.okf.md|Loading states]] — the one loading indicator (a bare turning ring), no loading sentence, no skeleton

## Entry points

- `ui/web/src/app/layout.tsx` — root layout
- `ui/web/src/app/page.tsx` — homepage (HomePage → HomeShell → HomeContent)
- `ui/web/src/components/shell/providers.tsx` — Provider composition + QueryClient
- `ui/web/src/lib/state/store.ts` — Zustand store (pure UI + cluster coordination, not persisted)
- `ui/web/src/lib/timeline/timeline-store.ts` — Zustand store (SSE-driven timeline state, independent from `store.ts`)
- `ui/web/src/lib/transport/useEventStream.tsx` — SSE Provider
