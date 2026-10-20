---
type: doc
title: Fleet View
description: The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`)—relationship graph, task graph, task board, the unified Inbox notice queue, and the force-parameter panel the two graphs share.
tags:
- frontend
- fleet
---

# Fleet View

## What it is

Full-screen supervision plane: judge 10-20 agents without opening any conversation. Read-only except InboxQueue's reply / Send / Dismiss (require_response) and Mark read (FYI) — the resurrect button was removed 2026-07-31. Desktop ≥lg left-right draggable split, mobile full-screen tabs.

## Sub-components

- **GraphView** (`graph-view.tsx`) — force-directed relationship graph: spawn/fork/resurrect lineage + attenuated message traffic. Every agent edge is solid and muted gray; message and lineage edges remain distinguished by opacity/width. Nodes use the same public lifecycle statuses as the sidebar; when intermediate parent agents terminate, live descendants re-parent to their nearest live ancestor to preserve lineage springs. It mounts the live + terminated roster queries itself (same caches home's `useAgents` seeds): the graph payload is live-only, so the terminated roster is the lineage walk's input for re-parenting through terminated intermediates even on a direct `/fleet` load.
- **TaskGraph** (`task-graph.tsx`) — task tree (child tasks only, Done/Canceled visibility toggle, count consistent with board; the count reports visible nodes, so it is smaller than the rows the API returned whenever Done/Canceled are collapsed). Left pane: Graph/Task tabs (persisted). The time window runs on `updated_at`, which any write moves — `meiva.tasks.log` included — so annotating a months-old task pulls it back into `24h` and shifts every count compared against an earlier read (2026-10-20: a note on #3813, a September task, did exactly that).
- **ForceGraph** (`force-graph.tsx`) — canvas shared by GraphView and TaskGraph. Its d3 zoom/pan behavior, transform state, reset, and box-fit math come from `lib/use-svg-zoom-pan.ts`, also used by the memory graph; each canvas retains its own layout timing and focus-neighbor bounds.
- **InboxQueue** (`inbox-queue/`) — right pane single unified stream: all open notices (require_response decisions + FYI) sorted by priority→blocking→created_at, grouped via `groupByTaskSubtree` using the seven-day metadata-only task summary window; each row states its obligation rung (Decision / FYI, with Blocking on the stalled ones) so the list alone says which rows wait on the user, and the rows differ further on the action side (shared `OpenNoticeDetail` branches on `require_response`: numbered single-/multi-select or open-question form+explicit Submit+Close without answering, legacy reply box+Send+Dismiss / Mark read); resolved history behind a collapsed disclosure; the mobile tab badge counts `useNotices()` — the panel's own feed — so the badge and the list can never disagree (it used to roll up `notices_awaiting_response` + `unread_notice_count` from the live agents only, a second derivation of the same fact). A conversation-to-Fleet link carries `agent_id`; after the first notice load, the queue scrolls to and briefly highlights that agent's first displayed open notice without changing display order, or stays at the default position when none exists. Collapsing the panel is the DB-backed `display.fleet_queue_collapsed` of `fleet-view.tsx`.
- **TaskKanban** (`task-kanban.tsx`) — task board (owner column shows agent label, stale-while-error retains last data).
- **ForceControls** (`force-controls.tsx`) — shared force-directed slider panel for Graph View / Task Graph; `useForceParams` DB-backed (`display.graph_force_params`, `useDebouncedSetting` throttles writes). **One tuning for both canvases** (2026-08-05): the Task Graph reads the same `display.graph_force_params` key as the Agent Graph — a separate `.v2` key was never populated, so the task graph fell back to `FORCE_DEFAULTS` whose weak repulsion + strong center pull let disconnected subtrees drift apart (tiny/cramped). Edge styling is a single gray for every kind (lineage + message), distinguished by opacity/width only.

## Layout timing on a large graph

A graph of 200 nodes or more (`layoutOptions` in `force-graph.tsx`) settles in
manual, time-budgeted slices instead of the live d3 timer: the simulation steps
under a per-frame budget and commits positions explicitly, twice early and then
every 32 slices. Below that threshold the live settle stays, because a small
graph animating into place is the point of it.

The reason is the render, not the physics. Each live tick commits a new position
map through React state, and each commit repaints the whole SVG: the preview's
1,949-node graph spent **4.4 s of main-thread time in its first six seconds** at
4x CPU throttle, and the profile attributed 4.6 s to native layout/style/paint
with only tens of milliseconds in JavaScript. `useForceLayout`'s manual path was
built for the memory graph earlier (task #4008); this is the same treatment for
the two Fleet canvases.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md|Frontend Components]] — the catalog this surface was split out of; the conversation view and settings/auth components live there.
- [[ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md|State Management]] — where the `display.*` settings above are persisted, and which selections stay per-device instead.

## Question interaction

`OpenNoticeDetail` shares one `NoticeQuestionForm` between Inbox and inspector.
Ordered choices use native radio/checkbox rows; their displayed number is the
immutable array position plus one, with Other last and numbered too. Selecting
Other reveals a required textarea; deselection retains its draft but excludes
it from submission. Optional notes explain selected answers separately.
Open questions use a growing textarea; Enter adds a newline. Every structured
answer requires an explicit Submit. Failed submissions retain input and reuse
the existing operation key; a 409 disables the stale question. Resolved history
shows the original numbered choices and the stored answer/Other/note.
Queue rows put a wrapping question title above agent/time/blocking context.
