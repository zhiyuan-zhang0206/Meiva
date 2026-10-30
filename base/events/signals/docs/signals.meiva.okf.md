---
type: doc
title: Signal state
description: One row per open condition, so a detector says "still happening" instead of appending another event.
tags: [contracts]
---

# Signal state

On 2026-10-29 the cluster wrote **1,877 OTLP export refusals in a day**. Each was a true
statement, each became a row in `telemetry_events`, and none of them could be retracted:
an append-only event can say that a condition happened, never that it stopped. Reading the
river, nobody could answer "is the collector still refusing right now" without counting
recent rows and guessing a window.

`signal_state` is the place that answers it. One row per open episode of
`(source, subject, condition)`: a detector that sees the same thing again **touches** the
row (newest reading, one more count) instead of appending another event, and a reconciler
**resolves** the row when the condition is gone. `telemetry_events` stays the evidence; this
table only carries state.

`base/events/signals` owns the two writes. Reads stay in SQL: nothing else writes these
rows, and there is no second way to change a state.

## The row

| Column | Meaning |
|---|---|
| `source` | who reports: a machine, a service, a subsystem |
| `subject` | what the report is about |
| `condition` | the stable name of the condition within that pair |
| `severity` | `info` / `warning` / `error`, repeated as a CHECK in the database |
| `opened_at` / `last_seen` | when the episode started, when it was last reported |
| `count` | how many reports this episode absorbed |
| `first_event_id` / `last_event_id` | the reports, as `telemetry_events` ids, no foreign key |
| `resolved_at` / `resolve_reason` / `evidence_ref` | how and why it closed, and what convinced the closer |
| `acked_by` / `acked_at` | a human read it — **not** a close |

The unique index is partial: `(source, subject, condition) WHERE resolved_at IS NULL`. A
closed row is history and blocks nothing, so the same condition closing and reopening
leaves a series of episodes instead of overwriting one row.

Three columns rather than one joined key: the reconciler, the reports and any per-source
query aggregate by `source`, and a joined string would have to be split again or scanned
whole.

## The two writes

```python
episode = open_or_touch(
    conn, source="macmini", subject="otlp-collector", condition="export_refused",
    severity=SignalSeverity.WARNING, event_id=event_id,
)

closed = resolve(
    conn, source="macmini", subject="otlp-collector", condition="export_refused",
    reason=ResolveReason.RECOVERED, evidence_ref="2026-10-30T02:11:04+08:00 export accepted",
)
```

`open_or_touch` is one statement: `INSERT ... ON CONFLICT (source, subject, condition)
WHERE resolved_at IS NULL DO UPDATE SET last_seen = now(), count = count + 1,
last_event_id = ...`. The conflict target repeats the index predicate because PostgreSQL
only infers a partial index when the statement names it.

`resolve` touches only the open row. Resolving an already-closed episode, or one that never
opened, returns `None`: a reconciler re-deriving the same verdict from a stale read is an
ordinary race, not a fault.

Neither function commits. Both run inside the caller's transaction, so a detector can write
its event and touch its signal in one commit — otherwise the state and its evidence drift
apart; and a caller that gives up half way leaves nothing behind.

## Rules worth knowing before you adopt it

- **The newest report decides the severity.** A condition that escalated is described by its
  last reading; the path it took is in the events. No hidden ordering logic in the row.
- **`count` counts calls, not distinct events.** A re-delivered event touches twice. The
  number is for "how noisy is this", not for accounting.
- **`evidence_ref` is written once, by the closer.** An open row has none; an episode that
  closed has the reason someone believed it.
- **An acknowledgement is not a resolution.** `acked_by` records that a human saw it; the
  condition keeps standing and the row stays open until the condition itself goes away.

## What this is not: the file-level alarms

`base/events/alarms` (2026-10-28) already records alarms — one JSON line per detection on
one machine, written before the send, readable when the gateway, database and network are
all gone. The two answer different questions and neither subsumes the other:

| | `base/events/alarms` (file) | `signal_state` (table) |
|---|---|---|
| question | did this detection reach anyone? | is this condition still true? |
| scope | one machine, one process | the cluster, merged by condition |
| available when | always — no socket, no config | the database is reachable |
| closure | none: only delivered / notified / acked | `resolve` is the point |

Deleting the file-level one would drop the only evidence that survives a database outage —
which is exactly when a delivery failure is likeliest. A detector that wants both writes the
alarm first and touches the signal second.

## Limits of v0

- **No column-level ACL.** The write entry point is the only writer *by convention*: any
  process holding database credentials can `UPDATE` these rows. Column grants are a
  revision-sized weapon, and this table does not earn them yet.
- **No reconciler yet.** A row closes when something calls `resolve`; nothing sweeps stale
  rows on its own. That is the next slice, and until it lands an abandoned episode stays
  open.
- **No consumer yet.** The table is written by nothing in this change: it exists so the
  detector changes that follow have one place to record state, instead of inventing four.
