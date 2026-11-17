# Frontend telemetry

Tracked interactions flow **one-way** to the gateway — never into React Query / SSE:

`lib/telemetry.ts` `track(element, {page, key, value, dedupe})` → in-memory buffer (dedupe 2s per page/element/key; Web Vitals and API timing opt out; under the 100/min per-tab and 200 pending caps) → batched `POST /api/frontend-telemetry` (sendBeacon on hide/pagehide, fetch+keepalive otherwise) → gateway validates (shape 422 / 64KB 413 / per-session 120/min backstop) → one `frontend_interaction` event per interaction (category=telemetry, source=user) → the unified event stream (Loki) → Grafana core panels.

Instrumented points: page views plus standard FCP/LCP/CLS/INP (`lib/telemetry-page-view.tsx` + `lib/web-vitals.ts`, mounted in AuthGuard's authenticated branch), API requests slower than 800ms (`lib/api.ts`, normalized numeric path segments), composer send-to-first-turn-start latency (`lib/interaction-timing.ts`), agent lifecycle actions (`lib/use-agent-actions.ts` onSuccess), message send/stop (`components/composer.tsx`), every user_settings change (`lib/use-user-settings.ts` setSetting), and one `sse-transport` event per authenticated tab session (`key=shared|fallback`, value carries secure-context/Web Locks/BroadcastChannel flags). No sensitive content: `element` is a closed union, keys are bounded normalized identifiers, and `value` is a ≤64-char scalar. The bounded shell-height diagnostic below uses a separate typed numeric payload.

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

## Bounded shell-height incident evidence

The home-shell diagnostic collects incident evidence to distinguish an html inline
pin from a shortened internal flex chain. Its desktop-shaped filter limits noise;
it does not classify on-screen keyboards. It changes no height, width or scroll
policy and is not a causal fix for the reported incident. No content, input values,
URLs, selectors, agent identifiers, DOM markup or screenshots are collected.

The second record prefers observed recovery over next-frame ordering evidence,
with a single ten-second deadline. Recovery after the deadline is not recorded.
Hidden/pagehide/unmount cancels pending observation without refunding the document
budget. An event-driven observer accepts that metric-only silent transitions and
paint faults with full DOM geometry can remain invisible. Delivery uses the shared
best-effort telemetry budget, so records can be dropped under load.

The existing OTLP event-body export preserves the declared numeric attributes in
Loki. No new index labels or storage are added. In Grafana Explore, use the existing
Loki datasource:

```logql
{event_name="frontend_interaction"} | json | attributes_element="viewport-anomaly"
```

Group parsed `attributes_session_id` and `attributes_geometry_incident` together,
then inspect the `attributes_geometry_*` numeric fields and phase. A short inline
pin with shortened boxes implicates the application pin path; full html/body with
a short shell points toward internal sizing. Geometry alone cannot establish a
paint/compositor fault. Generic interaction panels include these diagnostic records.
