# Frontend telemetry

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value, dedupe})` → in-memory buffer (dedupe 2s per page/element/key; Web Vitals and API timing opt out; under the 100/min per-tab and 200 pending caps) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → the unified event stream (Loki) → Grafana core panels.

Instrumented points: page views plus standard FCP/LCP/CLS/INP (`lib/telemetry-page-view.tsx` + `lib/web-vitals.ts`, mounted in AuthGuard's authenticated branch), API requests slower than 800ms (`lib/api.ts`, normalized numeric path segments), composer send-to-first-turn-start latency (`lib/interaction-timing.ts`), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), every user_settings change (`lib/use-user-settings.ts` setSetting), and one `sse-transport` event per authenticated tab session (`key=shared|fallback`, value carries secure-context/Web Locks/BroadcastChannel flags). No sensitive content: `element` is a closed union, keys are bounded normalized identifiers, and `value` is a ≤64-char scalar.

## Relationship to Other Nodes

- [Frontend Data Flow](frontend-data-flow.md) — the node this one-way flow departs from.

## Web Vitals interpretation

Measurement uses the official stable web-vitals library with default reporting.
CLS uses the largest session window; INP groups events by interaction and selects
the near-worst interaction according to interaction count. Hidden checkpoints
can report updated values after a tab returns; bfcache restores start fresh
metrics. Client route changes keep the document measurement alive, and each report
inherits the current normalized telemetry route. Unsupported browser APIs produce
no corresponding metric, and pages without interactions can omit INP.

The adapter sends only the existing fcp/lcp/cls/inp keys: milliseconds rounded to
integers, CLS rounded to four decimal places as a string. Entries, DOM targets,
navigation URLs and attribution stay out of the payload. Reports flush immediately
through the same bounded transport. Duplicate initialization is document-scoped,
including authenticated component remounts; there is no component-unmount cleanup.

The November 2026 calibration changes historical comparability: earlier CLS was
a lifetime sum, INP was a maximum event duration, and collection stopped at the
first hide. Segment analysis by the deployed revision. Repeated library reports
are checkpoints, not independent page-load samples; the existing scalar schema
has no metric ID for reconstructing one final value per navigation. Generic
Grafana interaction-volume/top-elements panels include these events, so their
counts may change with lifecycle coverage. The core frontend metrics and dashboard
currently count interactions rather than computing Web Vitals percentiles.
