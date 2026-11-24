---
type: doc
title: Telemetry and log events are read from Loki for seven days
description: Why /api/events and the admin events read Loki instead of telemetry_events, and how paging and filters changed.
tags: [telemetry, loki, events]
---

# Telemetry and log events are read from Loki for seven days

## Context

[`2026-10-03-telemetry-readers-on-postgres.md`](../../data/database/2026-10-03-telemetry-readers-on-postgres.md)
made `telemetry_events` the record every reader computes on, because Loki was a poor aggregation
engine (result caches, query budgets, an 84-hour retention clamp, a 90-day query ceiling). The
telemetry store design (`#8613`, 2026-11-24) removes `telemetry_events`: telemetry is lossy
operational data kept seven days, and the business records are `audit_events` and the usage ledger.
What still needs the telemetry and log events is looking at them: `meiva events`, `/api/events`
and `/api/cluster/admin/events`. Those reads are a filter and a count, not an aggregation, and the
retention is now seven days for every store.

## Decision

- The telemetry and log branch of those three reads goes to Loki; the audit branch stays on
  `audit_events`. The response shapes are unchanged except for paging.
- Paging is by time. `offset` and `meta.offset` are removed (Loki has none); `meta.next_to` is the
  `to` that asks for the next older page.
- Filters are written so that they hold for the whole seven-day window, which a measurement on the
  production Loki settled (see `gateway/events/docs/event-reads.md`):
  `level`, `category` and `trace_id` are structured metadata on every line, `agent_id` and
  `event_name` are index labels on every line, `machine` is an index label only from 2026-11-24 on
  and is read from the line body.

## Alternatives rejected

- **Keep `telemetry_events` for these reads.** It is the table the redesign deletes, and it only
  ever held the events some reader named plus warnings, so `/api/events?trace_id=` never returned a
  turn's whole chain; Loki does.
- **Filter `machine` by the index label and let it fill in.** Seven days from the label's start the
  gap closes, but until then a machine filter would silently miss older lines, and the code would
  carry a transition to delete later. Reading the body field costs a JSON parse of the matched lines
  (1.7 s over seven days) and needs no cleanup.
- **A bare regex alternation for the tier filter.** Loki 3 mishandles it when one literal is the
  prefix of another (measured: `sse` and `spawn` never matched), so every regex is anchored.

## Consequences

- A telemetry or log event older than seven days cannot be read; `/api/events` over a longer window
  returns what remains. A caller that needs a longer record emits an audit event or a ledger row.
- `/api/events` and the admin events depend on Loki being up: a Loki that cannot answer is a
  retriable 503, where the Postgres read had no such dependency.
- A page boundary can skip a row that shares the oldest row's microsecond and did not fit the page.
