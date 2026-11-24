# Event reads: audit from Postgres, telemetry and log from Loki

`GET /api/events`, `GET /api/events/counts` and `GET /api/cluster/admin/events` read two stores.
`audit_events` (Postgres, permanent) holds the `audit` category. Loki holds the whole event stream
for seven days (`EVENT_STREAM_RETENTION`), and the `telemetry` and `log` categories are read from
it; an older telemetry or log event does not exist any more. A request that names no category reads
both and merges the pages newest first.

## Files

- `events_plan.py` (strict) turns the request into a plan: validated filters, the stores, the
  window, the page, and `next_page_to`, where the next page starts.
- `loki_query.py` (strict) writes a plan's filters in LogQL (`LokiFilters` in, query text out).
- `loki_rows.py` sends the query to Loki and turns the answer into rows, counts and groups. The
  gateway builds one pooled `httpx.Client` at start-up (`MEIVA_TELEMETRY_LOKI_URL`, timeout
  `events_read_timeout_ms`); a Loki that cannot answer is a retriable 503
  (`gateway/lgtm/backend_failure.py`).
- `router.py` (`/api/events`, `/api/events/counts`) and `gateway/cluster/router.py`
  (`/api/cluster/admin/events`) execute the plan and merge.

## How a filter reaches Loki

Measured against the production Loki on 2026-11-24; the reasons are in
[the decision](../../../docs/decisions/observability/telemetry/2026-11-24-event-reads-from-loki.md).

| filter | LogQL |
|---|---|
| `agent_id` | index label in the selector; `agent_id=""` selects the events of no agent |
| `event_name` | index label in the selector |
| `level`, `category`, `trace_id` | structured metadata, a pipeline label filter (`\| level="warning"`) |
| `machine` | the body's field: `\| json event_machine="machine" \| event_machine="m1"`, because the `machine` index label exists only on lines from 2026-11-24 on |
| `tier` | a label filter over `level` and `event_name`, mirroring `tier_for` |
| `grep` | a case-insensitive literal line filter over the whole recorded line |

Every selector excludes the healthcheck's write probe (`agent_id="watchdog"`), which shares the
stream but is not an event and has no JSON body. A line that is not an event body fails the read
instead of being skipped.

Every regular expression is written `^(?:a|b|...)$`. Loki 3's pipeline label filter mishandles a
bare alternation in which one literal is the prefix of another: `event_name=~"sse|sse_drop|spawn|spawn_x"`
matches no `sse` line, so a tier built from the registry's 244 names silently lost `sse` and
`spawn`. The anchored form matches every line it should.

## Row identity, windows and pages

- A row's `id` is `blake2b(<Loki entry timestamp in ns>:<line>)` (`telemetry.event_id`) and
  `line_sha256` is the digest of the line, so a row has the identity the emitter gave it in the
  JSONL mirror.
- A Loki entry's timestamp is the event's `ts` after a float conversion, within about 250 ns of
  the line's own microsecond `ts`. A read pads its window by 500 ns, which includes every row of
  the window and excludes the neighbouring microseconds, and keeps the rows whose line `ts` lies
  in `from <= ts <= to`; that is what lets the next page (`to` one microsecond below the oldest
  row) exclude the row it came from without reporting a phantom further page.
- Loki has no offset. A page is the newest `limit` rows (one more is fetched to see whether older
  ones remain); `meta.next_to` is the oldest row's `ts` minus one microsecond and the next page is
  the same request with `to=meta.next_to`. A row that shares the oldest row's microsecond and did
  not fit the page is skipped.
- A total or a group count is an instant `count_over_time` query over a range in whole
  milliseconds (the unit Loki accepts), rounded up, so its lower edge is accurate to a millisecond.
  `by=level` and `by=category` name their label in a stage, which is what attaches structured
  metadata to the series; `by=machine` reads the body field.
- Measured cost on 2026-11-24 with 1.3 million lines in the seven days: a page 0.1 s, a seven-day
  count or grouped count 1.5 s, a seven-day `trace_id` search 1.2 s, a seven-day `grep` 3 s.
