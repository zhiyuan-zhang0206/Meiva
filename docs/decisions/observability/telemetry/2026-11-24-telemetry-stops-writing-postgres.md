---
type: doc
title: Telemetry and log events are stored in Loki only; Postgres and the local mirror stop receiving them
description: Why the event pipeline's only sink is now the OTLP exporter, what was deleted with the other sinks, and the release constraint that follows.
tags: [telemetry, loki, postgres, events]
---

# Telemetry and log events are stored in Loki only; Postgres and the local mirror stop receiving them

## Context

[`2026-10-02-telemetry-events-in-postgres.md`](../../data/database/2026-10-02-telemetry-events-in-postgres.md)
made `telemetry_events` the record of every telemetry and log event, with the JSONL mirror as its
fallback and a replay that repaired what the table missed. The later decisions narrowed what the
table stored and moved every reader onto it. The telemetry store redesign (2026-11-24) reverses that:
telemetry is lossy operational data kept seven days, the business records are `audit_events` and
`llm_usage_ledger`, and the readers have left the table one stage at a time. The last two stages
before this one moved the usage readers onto the ledger and
[the event reads onto Loki](2026-11-24-event-reads-from-loki.md). Nothing reads `telemetry_events` or the
mirror any more, so the writes only cost: a database connection and an insert per event in
every process, a second copy on local disk, and a replay pass that existed to reconcile the two.

## Decision

- The event pipeline has one sink, the OTLP exporter. `build_pipeline` takes no database; a process
  that holds no database login is no longer a special case.
- Deleted with the other sinks: the `telemetry_events` writer and its `persist` judgment
  (`EventSpec.persist` and its 24 declarations, the registry's `stored` column, the
  `telemetry_store_failed` event), the JSONL mirror's write, prune and replay (the events-maintenance
  pass and its `agent_metric_file_cursors` cursor), and the observed-metrics projection. The operator
  script that ranked SDK calls from copied mirror files goes too; the `sdk_call` events are ranked
  from Loki.
- Loss reports (`event_log_drop`, an exporter queue that overflowed) go to the process log and the
  loss metric. They were also appended to the mirror, which no longer exists, and the metric is what
  the cluster alert reads.
- `audit_events` is untouched: it is written in the producer's business transaction and Loki gets a
  projection of it.
- The tables stay in the schema until the contract stage ([note](../../../../future/infra/data/telemetry-events-drop.md)).
  No migration ships with this change.

## Alternatives rejected

- **Keep the mirror as a local fallback while the collector is unreachable.** Nothing replays it and
  nothing reads it; a fallback nobody consumes is a disk cost that looks like a safety net. The
  process log still holds every log line, and a collector outage is an alert, not a data recovery.
- **Stop writing and drop the tables in one release.** A rollback to a release that still inserts
  would fail on every batch. The drop waits for the stop-write release to run first.

## Consequences

- **Release constraint.** A release before this one still writes `telemetry_events` and still has
  readers of it, so this change can only be rolled back to the release that moved the event reads to
  Loki (`v2026.11.24.153933`), whose readers already use Loki and whose writers still fill the table.
  It is released once that release has run stably; an older rollback target would read an empty
  table.
- An event emitted while OTLP export is disabled (`MEIVA_TELEMETRY_OTLP_ENABLED=off`), or after an
  operator command stopped the local collector, or while the collector is unreachable and the
  exporter queue is full, is stored nowhere. Its log line is still in the process log.
- Old `~/.meiva/logs/events-*.jsonl` files are no longer pruned; the operator deletes them once.
