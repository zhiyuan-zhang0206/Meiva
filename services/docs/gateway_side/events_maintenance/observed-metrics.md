# Observed Inspector Metrics

The emitter also projects compact typed facts and day sums into Postgres. A projection write that fails is not retried or rebuilt: the event stays in `telemetry_events` but its observation is never written, so Inspector statistics are partial by that amount. `node_exit` is not stored in `telemetry_events`, so activity observations come from the live projection only. Inspector reads only Postgres; older full-day cost/turn ledgers remain disjoint historical evidence.

The gateway read boundary is [Agent Inspect Router](../../../../gateway/inspect/docs/inspect.md).
Each `llm_usage` row also carries its billing split: the price and the unpriced-call count of a
call whose `source` is a subscription harness (`HARNESS_BILLING`) are recorded in
`cost_usd_subscription` / `unpriced_calls_subscription`, the parts of the row's totals that the
Inspector shows as `Cost (subscription)` rather than `Cost (API)`. Rows projected before the split
read as all-API.
The producer is `base/telemetry/metrics/observed_metrics.py`; it commits fact identities and
additive daily sums together. The emitter isolates projection failure after
writing its local mirror, without creating recursive telemetry. Blank usage rows
(`unpriced_calls > 0` with `cost_usd = 0`) are later healed idempotently by
`services/upkeep/events_maintenance/unpriced_backfill.py` at `occurred_at` once
versioned pricing lands in `pricing_catalog_archive.json`; rows that already captured
a usage-time price are never repriced.

Collection is not lossless: an observation the live projection failed to write stays missing.
A populated downgrade
refuses before removing any table or trigger because finite source retention may
leave these facts as the only surviving evidence.
