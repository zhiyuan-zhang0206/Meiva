# Trace delivery topology

## Delivery topology

```
agent process ── OTLP/HTTP ──▶ local collector
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

## Traces are telemetry

A span has one store, Tempo, kept for seven days. There is no local copy and no
replay: standard OTLP/HTTP delivery is the whole path, and every exporter uses a
bounded in-memory sending queue, so a collector restart drops what was queued and
the memory limiter can return backpressure. The system makes no absolute no-loss
promise for traces, logs or metrics; what must not be lost (audit, usage and cost)
is business data written to the database, not telemetry.

Spans are metadata-only: LLM content is stripped at the source, and the
conversation itself lives in the checkpoints (see the Meiva Guide's
`troubleshooting` skill, `references/find-the-run.md`). The pre-compact conversation dump
(`agent/hooks/history_dump.py`, `MEIVA_COMPACT_HISTORY_DUMP`) is unrelated to
traces.

The producer-side contract is in [OTLP export backend](telemetry-otlp.md).
