# Metrics

## What is it

`base/telemetry/metrics/` holds two things: the metric definitions that render the Grafana
dashboard (`plugin_metrics.py`, `core/`, `grafana_dashboard.py`; Prometheus/Loki are the evaluators),
and one Postgres read, the per-agent rollups behind `GET /api/metrics/agents` and
`meiva metrics agents`.

## Per-agent rollups

`aggregate.fetch_agent_rollups` reads the usage ledger (`llm_usage_ledger`) in one statement and
returns one headline row per agent with usage in the window. Turn counts, exec outcomes and event
counts are not served: they came from `telemetry_events`, which has no consumer left. The
whole-fleet digest (`/api/metrics`), the per-agent `/api/agents/{id}/inspect/metrics` surface and the
`--since-compact` window were removed with those readers.

## Notes

- Cache-hit rates use the same calls in numerator and denominator: only calls with numeric `cache_read` metering. A reported zero remains a measured miss; absent metering contributes no cache-rate input. Nonzero usage without metering yields null/N/A while calls, tokens and cost stay complete within the observed window.

- `/api/metrics/agents` sums usage-time `cost_usd` snapshots, including cache-write costs and the served Standard/Fast rates. Recorded zero is priced; `unpriced=1` is counted without fabricating a cost. Token totals and monetary totals are reduced independently within the same agent/window scope.

## Key Dependencies

- [Database Layer](../../../../agent/db/docs/db.md) — Postgres pool (the usage ledger the rollups read)
- [Logging](../../../log/docs/log.md) — the event stream is written by the unified emitter (`base/telemetry/emitter.py`), fed by `base/log/process.py`

## SDK event collection

The SDK wraps its public static functions at import and after plugin loading; dynamic
MCP tools share a wrapped call funnel. Each public entry records independently,
including nested fan-out and recursion. Plugin layers retain one installed recorder
for their public function. Async wrappers measure the awaited call, including errors
and cancellation. The execution owner supplies its complete unsampled tally through
`MeivaContext.sdk_calls`; calls and ordinary threads share that owner explicitly.

`MEIVA_SDK_CALL_SAMPLING_ENABLED=false` is the default (every call). When enabled,
`MEIVA_SDK_CALL_SAMPLE_EVERY=N` sets inclusion probability 1/N (an independent draw per call from the process root's generator), with N >= 1. These
cluster fields use the existing config API/CLI. Processes start with their boot
policy and refresh active-call policy snapshots in the background every five
seconds from local `.env` on gateways and unenrolled tools, or the existing
authenticated bootstrap endpoint on an enrolled runner. Fetch failures warn and retain the last valid policy. SDK
calls never wait for a remote configuration fetch.

Events go directly to the unified emitter, so external callers need no logger
initialization or an execution tally. Call-time agent/source attribution snapshots
explicit SDK provenance, including borrowed identities, without validating a lease
or doing database I/O. Sampling is an explicit
loss of detail: sampled events cannot reconstruct a complete call history.

## Metrics test windows

`tests/telemetry_stream.py` writes append-only telemetry and passes a chosen
`now` into the read under test. Random slots are 40 days apart, wider than the
30-day read window, so different slots separate their fixture windows. The
sampling is with replacement; two streams can choose the same slot. This setup
reduces interference but does not guarantee unique windows or absolute isolation.

## Plugin query validation tests

`base/packages/tests/test_plugin_metrics.py` checks registry admission and the
SQL validator in `plugin_metrics.py` and `plugin_metrics_sql.py`. Table checks
recurse into scalar subqueries, including function arguments: nesting a
`pg_class` read inside a permitted function must not bypass validation. Legal
subqueries over `agents_meta` remain accepted. SQL templates are static, without
Grafana macros or template placeholders; event names are separate data and may
contain hyphens. Literal rendering escapes embedded quotes.

These tests validate query structure and registry rules. They do not execute
the sample SQL or establish that its referenced columns exist in the database.
