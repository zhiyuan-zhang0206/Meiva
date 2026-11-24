# Frontend State Management

One writer per cache/flag: TanStack Query owns server data and persisted preferences; Zustand and localStorage retain only the volatile/device state listed below.

## Three Mechanisms Division of Responsibilities

Termination toasts follow the response: `enqueued` means requested, not exited,
including force requests. Authoritative roster reads update lifecycle rows.

| Mechanism | Responsibility | File |
|---|---|---|
| TanStack Query | All **server data** (agent list, status, timeline snapshots, token, agent pages, inspect) **+ persistent UI preferences** (`display.*`/`behavior.*`/`feature.*` in `user_settings`, via `useUserSettings`/`useDebouncedSetting`); SSE merges into cache, no polling | `lib/use-*.ts` |
| Zustand `store.ts` | **Volatile UI state** (activeId, composer focus token, mobile drawer, toast, search) + cluster coordination (`reconnectNonce`); **not persisted** (`persist` middleware removed) | `lib/store.ts` |
| Zustand `timeline-store.ts` | **SSE-driven timeline state** (items/turnActive/streamingCode/streamingIds/hasMoreOlder and the compact display buffer); split from `store.ts` so high-frequency code_delta/chat_delta folding only notifies timeline subscribers, not sidebar/spawn/banner | `lib/timeline-store.ts` |
| localStorage | **Per-device selections** and library-managed panel widths. The [browser port](browser-port.md) documents how the app reads and stores them. | `use-agents.ts`, `home-layout.tsx` |

`display.*`/`behavior.*`/`feature.*` covers: inspector open state, timestamp/date/clock preferences, sidebar collapse/view mode/sort/stats/show terminated, Fleet graph layout controls, shell terminal theme, spawn model/reasoning_effort, notification and confirmation toggles, `behavior.replySuggestions`, cross-reload reading-position restore (`feature.reloadReadingRestore`, on by default in `Control -> Preferences -> Advanced`), one-realtime-connection (`web.sseSingleStream`, off by default) — defaults in `lib/contracts/types.ts:USER_SETTING_DEFAULTS`.

## Reaching the browser

Every read of the page itself — its address, its two storage areas, its media
queries, its clipboard, its unload-safe beacon, the view it scrolls — goes through
one port handed down from the composition root, never through `window`. A test
mounts `fakeBrowser()` and reads what the component asked for instead of stubbing
a global. [Browser capability port](browser-port.md)
owns the port, its three-answer storage reads, and the fake.

## Inspector open state

`inspector-panel-store.ts` is breakpoint-aware (task #793): on desktop (≥ lg) the inspector is the right panel's secondary view, so `display.inspector_open` stays a DB-backed workspace preference (default closed) that selects that view; on mobile (< lg) the reading surfaces are full-screen overlays that hide the timeline, so the inspector's open state is **per-session volatile state** (`mobileInspectorOpen` in `store.ts`, default closed) and mobile toggles never write the shared setting — opening or closing on a phone must not yank the desktop view. The statistics window select (`inspectorHours`) is session state and is not persisted.

Server data is not mirrored into Zustand — the sidebar reads `useAgents → useQuery`. The composer's drafted-reply ghost text is hook-local volatile state with one writer ([Reply Suggestion](../../../../../gateway/agents/docs/reply-suggestion/reply-suggestion.md)).

The agent tree owns one coherent live snapshot plus minimal ancestor links.
History is a separate paginated/searchable read with only its current page
retained. Selection uses an ID detail read when the agent is outside the live
roster; a terminated or bookmarked agent does not require fetching history.
Lifecycle SSE frames are ID hints, never unversioned full snapshots to replay
onto a newer database result. No terminated-agent cache accumulates events.

The global fold owns read-model repair. Hints coalesce under a fixed deadline;
reads never cancel an already-running repair, and hints received during that
read require a trailing repair. Every stream reconnect requests reconciliation,
including another disconnect inside a previous repair window. Settled query
keys release their scheduling state. The selected conversation trio (timeline /
token-usage / pending) reconciles under the same rules through one composed
read per re-attach (`agent-reconcile.ts`): it joins reads in flight, trails a
second gap during a read, and abandons queued and in-flight work when selection
or visibility is lost.

## Zustand `store.ts` (Pure UI + Cluster Coordination)

- **UI state**: `activeId`, composer focus token, mobile drawer, mobile inspector overlay (`mobileInspectorOpen`), toast, search. Spawn selections (`behavior.spawn_*`) and sidebar view mode/sort/stats (`display.sidebar_*`, hooks in `lib/sidebar.ts`) are **not here** — DB settings via `useUserSettings`.
- **Cluster coordination**: `reconnectNonce` is the SSE-reconnect lever. `AppConnectionBanner` reads connection health directly. No maintenance or update state is mirrored into Zustand; a reload while the app is down is answered by Gate's unavailable page.

## Selected Timeline State

The selected conversation store and its abortable reads have their own node:
[Selected Timeline State](timeline-cache.md).

## Sticky Bottom Controller (`lib/sticky.ts`)

The reader's place (the view anchor) and its one writer have their own
node: [Timeline sticky scroll](../frontend-components/timeline-view/sticky-scroll.md).

## History-Entry Scroll Memory

Per-container reading positions survive back/forward through the history
entry's `bfcacheId`; the save/restore path has its own node:
[History-Entry Scroll Memory](scroll-memory.md).

## Shared user context

The document reader, notification area, inspector view and file-change repair are
owned by [Shared user context](shared-context/shared-context.md).
