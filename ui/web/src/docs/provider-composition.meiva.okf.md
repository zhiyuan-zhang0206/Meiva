---
type: doc
title: Provider composition
description: Root provider tree in app/layout.tsx and components/shell/providers.tsx — QueryClient, EventStreamProvider, fold owner, root notice/toast hosts, VisualViewportHeightSync, and auth-gated health polling.
tags:
- frontend
---

# Provider composition

`<html lang="zh-CN">` (`app/layout.tsx`) → `Providers` (`components/shell/providers.tsx`: `QueryClientProvider` → `AuthProvider` → `EventStreamProvider` → [fold owner] → `AlertsProvider` → `ThemeProvider` → `AppConnectionBanner` + `ToastHost` + `OpenTasksNoticeHost`) → `AuthGuard` → page.

- **QueryClient**: `staleTime` default 5min, `gcTime` 30min, `refetchOnWindowFocus` off (SSE-driven queries set `staleTime: Infinity`; bounded snapshots opt into polling explicitly). Sidebar stats consumers share one QueryClient-level 30s poll coordinator, so responsive/header/footer observers cannot mint independent intervals. A 401 is never retried and globally invalidates the session so `AuthGuard` redirects to `/login` and unmounts polling observers (Task #1326).
- **EventStreamProvider** sits above the route tree: global `/api/system` broadcast persists across page navigation (one EventSource shared across routes; closed while the tab is hidden, reopened on return).
- **Fold owner** (`useFoldOwner`, inside `EventStreamProvider`): one subscriber turns global-broadcast events into coalesced Query invalidations (`["agents", "live"]`, agent directory/detail, notices, `["agent-pages", id]`, `["all-pages"]`, tasks, fleet graph) and runs the central reconnect reconcile. Page events refetch existing list queries and trail a GET in flight; absent keys are never seeded. Hooks only read their keys.
- **ToastHost**: root-level renderer for the store's toast slot, so error toasts reach the user on every route (not just Home).
- **OpenTasksNoticeHost** (#3374): root-level renderer for the store's open-tasks notice slot — a terminate response carrying `open_tasks` (wire #2488) raises a dialog (count + up to five rows + "and N more"); dismissal is explicit; lazily imported.
- **VisualViewportHeightSync** (#4779): with the on-screen keyboard up, pins `<html>` to `visualViewport.height`, publishes `--visual-viewport-height` and `--safe-area-bottom: 0px` on `<html>`, and resets root scroll to `(0, 0)` so fixed overlays, modals, and bottom bars stay inside the remaining area above an iOS/iPadOS `WKWebView` keyboard — the WebKit half of the contract whose Blink/Android half is `interactive-widget=resizes-content`. See [[ui/web/src/docs/mobile-and-tablet-ux.meiva.okf.md|Mobile, tablet and coarse-pointer UX]].
- **AppConnectionBanner** (#648): root-mounted and renders nothing; drives cluster health polling (`useClusterHealth`) and owns the store state consumed by `ConnectionNotice`; self-gated by `useAuth().status`. All Providers self-gate on auth state — no outer auth guard layer.

Root composition tests keep authentication pending and assert that alert reads,
SSE subscriptions and cluster polling stay blocked. Settings and theme readers
still run against explicit pending API boundaries. They mount the real alert
provider, observe the root query client and unmount before clearing its cache;
this is initial composition evidence, not live stream acceptance.

The connection notice counts failures only while the stream's page is visible.
Hiding cancels its 15s escalation timer and clears a previous error to
`reconnecting`. Returning, or initially mounting while visible, starts a fresh
15s recovery window; repeated errors and duplicate visibility events keep the
same deadline. An `open` clears the timer and error immediately, including when
it arrives before the notice's visibility listener. Hidden pages ignore stream
errors. Sustained visible failure displays an error while automatic transport
retries continue; Retry requests a local reconnect through `bumpReconnect`.
