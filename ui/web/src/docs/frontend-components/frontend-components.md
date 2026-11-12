# Frontend Components

## Conversation View (`app/page.tsx`)

Three layers: `HomePage` (read-only toast) → `HomeShell` (`useAgents`, activeId, lifecycle actions) → `HomeContent` (in `AgentEventStreamProvider`, with `useTimeline`/`useTokenUsage`/`usePendingMessages`). `HomeLayout` mounts after breakpoint settlement, adds draggable Agent Tree/Inspector splits with isolated desktop/compact autosave keys, and keeps phone overlays.

`HomeContent` keeps a failed timeline read visible with the original error and
manual Retry after automatic retries finish. A cold failure stays an error;
a failed refresh keeps loaded messages visible. Retry is disabled during the
read. `useTimeline` exposes the query error and refetch action without replacing
the cache or changing global retries. Separately retained checkpoint history
does not enter the live conversation through this surface.

- **AgentSidebar** — one header row of icons over the list, with display settings and creation behind popovers: [Agent Sidebar](agent-sidebar.md).
- **TimelineView** (`components/timeline/`) — the conversation timeline renderer; item kinds, dividers, cross-compact paging and deep collapse have their own node: [Timeline View](timeline-view/timeline-view.md).
- **Composer** — message input (file upload, content blocks, multimodal image_url); `disabled|idle|busy` states; busy reveals Stop (observes the agent's native work, then cancels it). One logical submit owns one client message id across timeout/retry and tab persistence. An unresolved delivery preserves the editable draft and reports an unconfirmed receipt through the existing error toast. Normal Send reuses the client message id for an unchanged draft; editing the content or attachments starts a new logical submission; storage failures degrade to an in-memory identity instead of blocking the send/spinner cleanup. `busy` unions `status='running'` with the SSE turn-in-progress signal, which alone flips at every mid-turn `llm_done`. A message invoking several commands (`/recap … /compact`) is sent whole — the server expands them inside that one inbound. The in-flight window never flips the textarea to `readOnly`/`disabled`: on Android that tears down the IME connection, so the next voice-input or paste commit is lost silently (user report 2026-10-19). Submission and attachments are gated instead, and a successful send clears the box only while it still holds exactly what was sent. Enter sends and Shift+Enter breaks the line here and in every other send-type field (question replies, question answers, the fork/resurrect prompt): [Enter sends](enter-to-send.md).

The Composer reads drafts and submit attempts directly from `conversation/composer-session.ts`,
which owns the browser session storage and its single in-memory fallback. JSX
rows, attachment presentation and keyboard handling stay in `composer-helpers.tsx`.

- **InspectorPanel** — the selected agent's current state, window statistics and plugin widgets read independently, only while open, and never on hover or selection. Each section loads, fails and retries on its own, renders nothing when empty, and its payload must match the selected agent/window before it shows; matching data survives a refresh failure quietly; cold failures render `InlineError`. Sections (run link → Cost + Activity → the runtime sections), the responsive close surfaces, and the per-agent retention and refresh ownership are in [Inspector Panel](inspector/inspector.md) and [reads and refresh](inspector/queries-and-refresh.md). The Inspector's Context gauge opens **ContextBreakdown** (`context-breakdown.tsx`, #650) — code-split until expansion while the live `ContextMeter` (`w-32` comfortable width) stays in the panel — and the same body renders as a card on the run page (the composer carries no context meter; user ruling 2026-11-01): [Context breakdown presentation](context-breakdown.md).
- **HeaderBar** / **ConnectionNotice** / **PendingStrip** — current-agent title, the PanelLeft sidebar toggle (carrying the unresolved-alert count while the sidebar is collapsed, nothing at zero) and, on desktop with an agent, the PanelRight context toggle; neither the composer nor Display settings carries a Details mode toggle (settled turns always start collapsed and live trailing turns auto-expand; user ruling 2026-11-01); the floating SSE connection notice below HeaderBar (`top-[52px] z-30`, escalating after 15s sustained disconnect); the strip lists only user-originated queued messages — an agent message, a watcher / shell / schedule wake or a system note never appears (the gateway stamps each row's `sender`, a closed category of its `source` tag, so the frontend selects without parsing tags) — and hides messages the timeline already shows (an external-harness chat stays in the pending list until the provider confirms it). There is no React updating component/state: update hints reload through the always-up Gate, which solely renders maintenance. **CopyButton** (`copy-button.tsx` + `lib/clipboard.ts`)—corner copy for a code block whose surface offers it (the timeline's tool-call and tool-output blocks turn it off); `execCommand` fallback when Clipboard API fails.
- **SpawnButton** — cross-machine placement, model and effort selection, mounted in the create popover and the empty-roster CTA; placement and selected-agent reason: [Creation Availability](creation-availability/creation-availability.md).

Warnings / Errors status card: [Warning and Error Counts](alert-classes.md).

Plugin status cards use full-width rows: primary `value`, secondary `detail`,
both wrap and preserve line breaks. No provider parsing. The sidebar popover
fits the viewport and scrolls within available height; empty/error/stale states
and update-age tooltips remain.

## Home shell chrome

The three columns' title bars, bottom rules, and edge-pinned controls:
[Home shell chrome](home-shell-chrome/home-shell-chrome.md).

## Code blocks

`PythonCode`'s highlighter loading — the lazy import, its proactive warm-up
and prefetch triggers, and when tokenization runs — has its own node:
[Code blocks](code-blocks.md).

## Fleet View

The full-screen supervision surface (`components/fleet/`, `app/fleet/page.tsx`) — relationship graph, task graph, task board, shared force controls — has its own node: [Fleet View](fleet-view.md).

## Settings / Auth

- Control page routing see [Data Flow](../frontend-data-flow/frontend-data-flow.md) hooks (`useUserSettings`, etc.); `/control/display` goes through the server-side `user_settings` table.
- **AuthGuard** (`components/auth/`) wraps all pages, showing a login page when unauthenticated; `auth-context` shared via React Context; on mount, only when the URL fragment carries a one-time `#ticket=` login link, it lazily loads `lib/auth/login-ticket.ts` (kept out of the first-load bundle), which clears the fragment before posting the ticket; `/api/auth/check` then decides, and a refused link leaves the login form (the web login password still works).

The run timeline page (`/insights/run/{id}`) is its own node: [Run Timeline](run-timeline/run-timeline.md).
