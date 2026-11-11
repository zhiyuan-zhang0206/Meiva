# Fleet View

## What it is

Full-screen supervision plane: judge 10-20 agents without opening any conversation. Read-only — the resurrect button was removed 2026-07-31 and the Inbox page 2026-11-11 ([decision](../../../../../docs/decisions/frontend/ui/2026-11-11-remove-inbox-and-counts.md)); a question is answered in its agent's conversation. Desktop ≥lg the graph panel fills the page, mobile has full-screen Agents and Tasks tabs.

## Sub-components

- **GraphView** (`graph-view.tsx`) — force-directed relationship graph: spawn/fork/resurrect lineage + attenuated message traffic. Every agent edge is solid and muted gray; message and lineage edges remain distinguished by opacity/width. Nodes use the same public lifecycle statuses as the sidebar; when intermediate parent agents terminate, live descendants re-parent to their nearest live ancestor to preserve lineage springs. It mounts the live + terminated roster queries itself (same caches home's `useAgents` seeds): the graph payload is live-only, so the terminated roster is the lineage walk's input for re-parenting through terminated intermediates even on a direct `/fleet` load.
- **TaskGraph** (`task-graph.tsx`) — task tree (child tasks only, Done/Canceled visibility toggle, count consistent with board; the count reports visible nodes, so it is smaller than the rows the API returned whenever Done/Canceled are collapsed; cold failures render `InlineError` and background refresh errors retain cached tasks quietly without stale badges). Left pane: Graph/Task tabs (persisted). The time window runs on `updated_at`, which moves only when the work changes (status, results, owner, priority, parent) — a note, a rename or a description edit leaves it alone, so annotating a months-old task no longer pulls it back into `24h`. `in_progress` tasks are kept regardless of age, so the window only decides which done/cancelled tasks stay visible. Before task #5311 any write moved it, and a note on #3813 (a September task) did exactly that on 2026-10-20.
- **ForceGraph** (`force-graph.tsx`) — canvas shared by GraphView and TaskGraph; hover card geometry, safe-zone bridge, dark-mode edge legibility, and large-graph settle timing live in [Force Graph](force-graph.md).
- **TaskKanban** (`task-kanban.tsx`) — task board (owner column shows agent label, stale-while-error retains last data).
- **ForceControls** (`force-controls.tsx`) — shared force-directed slider panel for Graph View / Task Graph; `useForceParams` DB-backed (`display.graph_force_params`, `useDebouncedSetting` throttles writes). **One tuning for both canvases** (2026-08-05): the Task Graph reads the same `display.graph_force_params` key as the Agent Graph — a separate `.v2` key was never populated, so the task graph fell back to `FORCE_DEFAULTS` whose weak repulsion + strong center pull let disconnected subtrees drift apart (tiny/cramped). Edge styling is a single gray for every kind (lineage + message), distinguished by opacity/width only.

## Relationship to Other Nodes

- [Force Graph](force-graph.md) — hover card placement, safe-zone bridge, edge legibility, navigation prop wiring, and 200-node settle timing.
- [Frontend Components](frontend-components.md) — the catalog this surface was split out of; the conversation view and settings/auth components live there.
- [State Management](../frontend-state/frontend-state.md) — where the `display.*` settings above are persisted, and which selections stay per-device instead.

## Question interaction

`QuestionsForm` renders one `QuestionForm` per open question, in one form, in the question bar above the composer.
Questions have three types: text, single select and multi select. Ordered choices
use native radio/checkbox rows, numbered by their immutable array position.
Single and multi select both show an optional comment textarea at all times.
Selection and comment are independent; a comment never replaces a required choice.
There is no Other choice or Add note toggle. Text questions use a growing textarea
and the comment box follows it; in both, Enter sends the answer and Shift+Enter
adds a newline — the shared rule in
[Enter sends](enter-to-send.md).
The Submit button stays for a pointer and for touch, where Enter breaks the line.
Failed submissions retain input and reuse the existing operation key; a 409
disables the stale question. A question card shows no priority, blocking or expiry:
every question is P2, never blocking and never expires.
