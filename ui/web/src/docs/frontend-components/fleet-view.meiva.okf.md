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

Full-screen supervision plane: judge 10-20 agents without opening any conversation. Read-only except InboxQueue's answer and explicit Close without answering — the resurrect button was removed 2026-07-31. Desktop ≥lg left-right draggable split, mobile full-screen tabs.

## Sub-components

- **GraphView** (`graph-view.tsx`) — force-directed relationship graph: spawn/fork/resurrect lineage + attenuated message traffic. Every agent edge is solid and muted gray; message and lineage edges remain distinguished by opacity/width. Nodes use the same public lifecycle statuses as the sidebar; when intermediate parent agents terminate, live descendants re-parent to their nearest live ancestor to preserve lineage springs. It mounts the live + terminated roster queries itself (same caches home's `useAgents` seeds): the graph payload is live-only, so the terminated roster is the lineage walk's input for re-parenting through terminated intermediates even on a direct `/fleet` load.
- **TaskGraph** (`task-graph.tsx`) — task tree (child tasks only, Done/Canceled visibility toggle, count consistent with board; the count reports visible nodes, so it is smaller than the rows the API returned whenever Done/Canceled are collapsed). Left pane: Graph/Task tabs (persisted). The time window runs on `updated_at`, which moves only when the work changes (status, results, owner, priority, parent) — a note, a rename or a description edit leaves it alone, so annotating a months-old task no longer pulls it back into `24h`. `in_progress` tasks are kept regardless of age, so the window only decides which done/cancelled tasks stay visible. Before task #5311 any write moved it, and a note on #3813 (a September task) did exactly that on 2026-10-20.
- **ForceGraph** (`force-graph.tsx`) — canvas shared by GraphView and TaskGraph; hover card geometry, safe-zone bridge, dark-mode edge legibility, and large-graph settle timing live in [[ui/web/src/docs/frontend-components/force-graph.meiva.okf.md|Force Graph]].
- **InboxQueue** (`inbox-queue/`) — response-required notices sorted by priority, blocking and creation time, grouped via `groupByTaskSubtree` using the seven-day metadata-only task summary window. Priority P0–P3 and blocking are independent. Every row needs an answer (no redundant Decision/FYI kind label); ordinary information belongs in chat. The shared `OpenNoticeDetail` provides the question form and a secondary Close without answering (`Dismiss`) action below the reply input. Resolved history stays behind a collapsed disclosure. The mobile badge counts the same `useNotices()` response queue. Conversation links select and highlight the agent's first displayed notice without changing order. `display.fleet_queue_collapsed` persists panel visibility.
- **TaskKanban** (`task-kanban.tsx`) — task board (owner column shows agent label, stale-while-error retains last data).
- **ForceControls** (`force-controls.tsx`) — shared force-directed slider panel for Graph View / Task Graph; `useForceParams` DB-backed (`display.graph_force_params`, `useDebouncedSetting` throttles writes). **One tuning for both canvases** (2026-08-05): the Task Graph reads the same `display.graph_force_params` key as the Agent Graph — a separate `.v2` key was never populated, so the task graph fell back to `FORCE_DEFAULTS` whose weak repulsion + strong center pull let disconnected subtrees drift apart (tiny/cramped). Edge styling is a single gray for every kind (lineage + message), distinguished by opacity/width only.

## Relationship to Other Nodes

- [[ui/web/src/docs/frontend-components/force-graph.meiva.okf.md|Force Graph]] — hover card placement, safe-zone bridge, edge legibility, navigation prop wiring, and 200-node settle timing.
- [[ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md|Frontend Components]] — the catalog this surface was split out of; the conversation view and settings/auth components live there.
- [[ui/web/src/docs/frontend-state/frontend-state.meiva.okf.md|State Management]] — where the `display.*` settings above are persisted, and which selections stay per-device instead.

## Question interaction

`OpenNoticeDetail` shares one `NoticeQuestionForm` between Inbox and inspector.
Questions have three types: text, single select and multi select. Ordered choices
use native radio/checkbox rows, numbered by their immutable array position.
Single and multi select both show an optional comment textarea at all times.
Selection and comment are independent; a comment never replaces a required choice.
There is no Other choice or Add note toggle. Text questions use a growing textarea
and the comment box follows it; in both, Enter sends the answer and Shift+Enter
adds a newline — the shared rule in
[[ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md|Enter sends]].
The Submit button stays for a pointer and for touch, where Enter breaks the line.
Failed submissions retain input and reuse the existing operation key; a 409
disables the stale question. Resolved history preserves original choices and
answers, including legacy Other text. Queue titles wrap above agent/time/blocking context.
