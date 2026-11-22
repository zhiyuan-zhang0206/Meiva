# Fleet View

## What it is

Full-screen supervision plane: judge 10-20 agents without opening any conversation. Read-only — the resurrect button was removed 2026-07-31 and the Inbox page 2026-11-11 ([decision](../../../../../docs/decisions/frontend/ui/2026-11-11-remove-inbox-and-counts.md)); a question is answered in its agent's conversation. The relationship graph fills the available space on desktop and mobile.

## Sub-components

- **GraphView** (`graph-view.tsx`) — force-directed relationship graph: spawn/fork/resurrect lineage + attenuated message traffic. Every agent edge is solid and muted gray; message and lineage edges remain distinguished by opacity/width. Nodes use the same public lifecycle statuses as the sidebar; when intermediate parent agents terminate, live descendants re-parent to their nearest live ancestor to preserve lineage springs. It mounts the live + terminated roster queries itself (same caches home's `useAgents` seeds): the graph payload is live-only, so the terminated roster is the lineage walk's input for re-parenting through terminated intermediates even on a direct `/fleet` load.
- **ForceGraph** (`force-graph.tsx`) — canvas rendered by GraphView; hover card geometry, safe-zone bridge, dark-mode edge legibility, and large-graph settle timing live in [Force Graph](force-graph.md).
- **ForceControls** (`force-controls.tsx`) — force-directed layout sliders. `useForceParams` stores the tuning in `display.graph_force_params` using debounced writes.

## Relationship to Other Nodes

- [Force Graph](force-graph.md) — hover card placement, safe-zone bridge, edge legibility, navigation prop wiring, and 200-node settle timing.
- [Frontend Components](frontend-components.md) — the catalog this surface was split out of; the conversation view and settings/auth components live there.
- [State Management](../frontend-state/frontend-state.md) — where the `display.*` settings above are persisted, and which selections stay per-device instead.

## Question interaction

`QuestionsForm` shows one open question at a time above the composer. Left and right
controls switch between questions. Submit sends the whole queue in one request.
Questions have three types: text, single select and multi select. Ordered choices
use native radio/checkbox rows, numbered by their immutable array position.
Single and multi select both show an optional comment textarea at all times.
Selection and comment are independent. A comment alone is also a valid answer.
Text answers and comments use growing textareas. Enter activates the shared Submit action, Shift+Enter adds a line break, and
Submit sends the batch.
Failed submissions retain input and reuse the existing operation key; a 409
disables the stale question. A question card shows no priority, blocking or expiry:
every question is P2, never blocking and never expires.
