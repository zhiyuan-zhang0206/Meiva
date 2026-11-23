# Trace mirror and delivery topology

## Delivery topology

```
agent process ── OTLP/HTTP ──▶ local collector ──▶ local JSONL trace mirror
                                      │
pure runner collector ── Bearer ──────┤ gateway collector ingress
                                      ▼
gateway collector ──▶ loopback Tempo + Loki + Prometheus ──▶ Grafana / read paths
```

The collector keeps network retries, backpressure and backend credentials out
of agent processes: the local receiver is the only endpoint an agent process
knows. A pure runner relays to one authenticated gateway ingress; the
native Tempo, Loki, and Prometheus endpoints bind to loopback by default.
`meiva-root` owns Tempo on the observability station. The collector sends
traces to `MEIVA_TELEMETRY_TEMPO_ENDPOINT` (default loopback port 14318), and
Grafana and Prometheus use `MEIVA_TELEMETRY_TEMPO_QUERY_URL` (default loopback
port 3200). The [native backend stack](../../../../../deploy/lgtm/README.md)
supports gateway ops and inspect endpoints, alerting, and events maintenance.
Tempo is the cluster's [trace viewer](../../../../../docs/decisions/observability/telemetry/2026-08-11-otel-viewer-selection.md).

## Why the mirror is JSONL

The mirror is a recovery and inspection copy, not the live path; standard
OTLP/HTTP is the live network form. Each line is one standard OTLP/JSON
`ExportTraceServiceRequest`, the shape any OTLP backend ingests, so no custom
format exists to maintain. The collector's file exporter writes it in parallel
with live delivery. Every exporter uses a bounded in-memory sending queue, so a
collector restart drops what was queued and metrics may shed stale points; the
memory limiter can return backpressure before the mirror. The system makes no absolute no-loss
promise.

It is distinct from the pre-compact conversation dump
(`agent/hooks/history_dump.py`): that file holds full conversation content for
audit and replay (`MEIVA_COMPACT_HISTORY_DUMP`), while the trace mirror is
metadata-only OTel with LLM content stripped at the source.

## Who consumes the mirror

1. `meiva trace ship` — recovery replay to Tempo (watermark-resumable, windowed
   backfill, idempotent). It bypasses the local collector so a replay cannot
   write itself back into the mirror. It is an operator command; nothing
   schedules it.
2. `fetch_trace.py --source mirror` in the `inspect-a-trace` skill — complete
   offline span retrieval with no size cap and no network (Tempo's full-trace
   API fails above its cap).
3. Nothing else reads it in-repo. Retention defaults to three days
   (`MEIVA_TRACE_RETENTION_DAYS`) and is pruned on agent start.

The producer-side contract is in [OTLP export backend](telemetry-otlp.md).
