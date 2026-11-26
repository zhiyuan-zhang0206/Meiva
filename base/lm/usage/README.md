# Durable LLM Usage

`base/lm/usage/` commits each reported usage unit to `llm_usage_ledger` in Postgres. It also emits `llm_usage` events and billing spans for the existing observation path. It is the shared accounting boundary for both LangChain messages and provider-specific raw usage fields.

## Usage emitters

- `log_usage_from_message()` is the accounting path for completed LangChain messages; it records the usage-time price snapshot, then emits the matching event and billing span.
- `log_usage_fields()` serves non-LangChain providers such as the Gemini embedding REST adapter and `decide()` (decision models, `tok_out=0`; see [Decision Models](../docs/decision-models.md)). `for_agent_id` explicitly attributes a daemon-generated event to its target agent; `source` labels the shared caller.
- `usage_kind` distinguishes agent, chat, batch, and embedding consumption. `source` is an optional payload field for shared text callers (`web.fetch`, `understand` for text, and `understand.<type>` — `image`, `pdf`, `audio` or `video`, the richest type of the content — for media) and is kept distinct from the transport provenance column.
- `log_usage_from_message(..., stamp_message=True)` (the main-conversation `log_llm_usage`) also writes the event's figures to `msg.additional_kwargs["meiva_usage"]`: one `quote`, one dict, two destinations.
- Gemini chat calls use ordinary implicit-cache accounting; `cache_read` is the provider-reported cached input.
- Cache fields are absent when a provider supplies no cache metering (including current decision and embedding calls). A reported `cache_read=0` is a measured miss; absence is N/A, never an inferred miss. Usage, price, latency and billing still record every completed call. Cache rates use only input from the calls reporting cache reads in both numerator and denominator.
- Previously emitted synthetic zeroes remain historical observations; they are not rewritten as unknown. Inspector projections preserve the legacy denominator for old rows without the new measured-input counter.

## Direct callers

`build_chat_model(...)` meters its own calls: `base/lm/usage/metering.py` attaches one LangChain callback handler to the built model, and every completed provider call (`invoke`, `ainvoke`, `stream`, `astream`, `batch`) writes the same `llm_usage` event `log_usage_from_message` writes, with `usage_kind="direct"`. Before this, a worker or script that built a model and called it directly spent money with no row at all (task #5327).

Exactly one layer owns a call's row. The factory owns it by default. A caller that emits the row itself — with its own `usage_kind`, `source` or agent id — builds with `meter=False`, or strips the meter from an injected build with `unmeter(model)`. The meter also stamps `msg.additional_kwargs["meiva_usage"]`, and `log_usage_from_message` refuses a message that already carries the stamp, so a caller that logs the same response cannot produce a second row.

`attribution` says how a row's agent identity was established: `caller` (the emitter supplied `for_agent_id`), `process` (the identity bound at telemetry startup), or `unattributed` (no identity was available, `agent_id` is NULL). Each exhausted ledger write or conflicting replay emits one `llm_usage_ledger_write_failed` warning. The direct write path propagates that failure. The receipt consumer retains the affected receipt. The OTLP logs lane sends the full event to Loki. Count exhausted writes and replay conflicts in Grafana with LogQL `sum(count_over_time({event_name="llm_usage_ledger_write_failed"}[5m]))` and inspect `reason`, `error_type`, and `call_id` in the event payload. It is a log-derived count under the explicit metric allowlist.

## Notes

- Usage events keep price snapshots at the time of use so cost accounting remains stable when catalog rates later change.
- Cache-write counts (`cache_write_5m`, `cache_write_1h`, `cache_write_30m`) and declared rate snapshots (`price_write_5m`, `price_write_1h`, `price_write_30m`) preserve TTL-specific creation costs. Writes are included in `in_total`, so they are deducted from ordinary input, priced once, and also recorded as additive billing-span attributes. Actual served Standard/Fast identity selects all rates together. Existing persisted cost snapshots are not rewritten.
- Calls without provider usage metadata emit nothing, except raw-field callers that intentionally account for a completed zero-token provider response.
- For a requested Fast ID, `usage_model()` reads the provider's actual service
  receipt before pricing: OpenAI `fast` / legacy `priority` and Claude `fast`
  keep the Fast ID; OpenAI `default` / Claude `standard` select the base ID.
  Missing or unknown receipts raise instead of inventing a premium charge.
  `llm_usage.model` records the served ID and `requested_model` preserves the
  selected ID. Compaction also passes the selected Meiva ID rather than the
  client's wire model name.
- Cache and reasoning counts include LangChain's tier-prefixed detail keys,
  such as `priority_cache_read` and `priority_reasoning`.

- Key deps: [Language Model Provider Layer](../docs/lm/lm.md) (provider-layer overview) and [LLM Billing Catalog](../pricing/docs/pricing.md) (price selection).

## Database commit and history

Cluster process boot supplies the database and machine identity. Direct usage writers
commit before the caller advances, using a separate transaction where the
caller has no checkpoint transaction to share. The first committed report owns
its time, machine and price snapshot. A replay of matching usage acknowledges
that row. Conflicting provider facts raise. Meiva receipts use the provider and
its request or response ID from `response_metadata`. The graph assigns its own
message ID separately. Direct callbacks use the LangChain run ID and input
index. A harness turn uses the provider session, the process generation persisted
in the session registry, the local turn ID, and the step index. The generation
advances when the same session resumes in a new process.

The writer retries a transient database failure once with the same frozen
report. On the direct path, an exhausted commit still emits `llm_usage` with the same ID, original
occurrence time, and exact quoted cost for historical recovery. The error
propagates after a completed harness turn finishes projecting its output. A
process killed after the provider bills and before Postgres commits can leave a
missing report. Use the provider bill to reconcile that window.

`calls` counts the calls described by a report. A harness can report one step
or a turn total. `complete` records whether the provider covered the usage.
API and subscription reports carry different billing labels and use the same
usage-time USD list prices. Subscription prices represent virtual spend.

The ledger started from the retained `llm_usage` telemetry: the one-time import
of 2026-09-26 through 2026-11-24 (1,177,185 reports; `historical_catalog` marks
the prices reconstructed from the versioned archive) was reconciled by UTC day,
reported model and billing before the readers moved to the ledger, with no
differences. Its commands are retired. Usage from before the ledger began (the first per-call report, 2026-09-26) was imported once, on
2026-11-24, from the retired day tables: 10,659 (UTC day, agent, model) rows from 2026-05-24 to
2026-09-25, 1,263,407 calls, all API billing, USD 3,698.46. Each row is one report at the start of
its UTC day: `call_id` `legacy-daily:<day>:<agent>:<model>`, `usage_kind` `legacy_daily` (a reader
that needs per-call detail excludes it), `calls` the day's `llm_calls`, and the stored cost snapshot
(`price_source` `legacy`; the hourly repricing pass skips `legacy_daily` rows, because a day's
tokens summed together cannot be priced by a per-call tier). The dry run found no row mixing API and
subscription calls and none partly priced. The day tables were archived and dropped afterwards.
Reports have no retention expiry; the
Loki `llm_usage` stream is the operational copy and keeps seven days.

## Replayable usage receipts

The gateway and indexer own a `UsageReceiptDelivery` for their explicit home and
ledger. Each starts the consumer with the service, including when it uses a
different embedding backend. Overlapping Gateway lifespans keep the same
receipt workers until the last lifespan exits. Gateway shutdown closes
admission and asynchronously allows eight seconds for staging and one second
for Postgres delivery. The staging budget covers a five-second SQLite lock
wait with time for commit and cleanup. If staging remains unconfirmed, shutdown
logs every affected receipt's full frozen payload before releasing the service.
A write already in progress may commit after the diagnostic, and replay of the
same `call_id` remains idempotent. Receipts already staged survive process exit.

Gemini passes the service's `UsageReceiptSubmitter` to `log_usage_fields`.
Before making an async provider request, Gemini reserves one of the service's
256 outstanding staging slots. This bounds memory during sustained local I/O
failure and rejects new work before it incurs a provider charge. Immediately
after a successful response has been parsed, Gemini freezes the ledger entry
and queues it on the dedicated staging thread before the next cancellable await. The request's
shielded wait can end when the request is cancelled. The service retains the
accepted job and completes staging independently of the default executor.
Indexer batch staging runs on its existing synchronous caller thread. Both
paths emit the usual usage event and billing span for the provider response.
Postgres delivery uses the original occurrence, attribution, machine, tokens,
exact decimal cost, price snapshot and `call_id` and emits no second usage
observation.

The SQLite spool lives at `$MEIVA_HOME/usage-receipts.v1.sqlite3` under the
same service home used to bind the ledger. The producer limits admission to
4,096 rows of up to 8 KiB, including quarantined rows, and limits the database
to 32,768 pages. These defaults bound the footprint of a prolonged outage
while reserving space for a substantial local backlog. Concurrent writers
share admission through a SQLite write transaction in WAL mode. Each staged
write can wait up to five seconds for a lock. Three bounded backoff retries
cover transient `SQLITE_BUSY` and `SQLITE_LOCKED` errors, including concurrent
spool initialization. Each consumer processes
at most 16 rows per batch. Submissions wake the local consumer immediately.
It rescans at least every 30 seconds when idle, which finds receipts from
other processes even if their producers exited without signalling.

- A successful insert or matching replay acknowledges the staged row. If a
  producer or consumer stops after Postgres commits, the next consumer gets
  the same acknowledgement by `call_id`.
- A conflicting or invalid row remains quarantined for inspection. Other rows
  proceed. A transient or unknown failure keeps the row pending and advances
  the scan cursor so later rows receive an attempt. The consumer backs off
  for 30 seconds per failed batch to limit database reconnections.
- `llm_usage_ledger_write_failed` counts failures in Loki. Spool errors state
  the safe error type or errno. Backlog warnings show pending rows, oldest
  occurrence time and available slots. A receipt from a different cluster
  remains pending for its ledger owner.

### Recover a receipt logged during a storage failure

`submit` returns `staged` after durable admission or `fallback_logged` if a
storage or capacity failure required the log fallback. The same recovery
record is emitted for each accepted entry whose staging remains unconfirmed
at the end of Gateway shutdown. The fallback record's
`receipt_json` is the complete frozen ledger payload. It excludes the Gemini
API key, request text and returned vectors. Extract that field as raw JSON from
the failure log. Once storage is healthy and capacity is available, run the
following from the deployed checkout with its Python environment, supplying the
actual service home and ledger cluster as arguments. Manual recovery can
admit a receipt larger than the automatic 8 KiB limit. The spool's row count,
ownership and SQLite page limits still apply.

```bash
python -c 'import sys; from pathlib import Path; from base.lm.usage.receipt import UsageReceiptStore; UsageReceiptStore(Path(sys.argv[1]), sys.argv[2]).recover(sys.stdin.buffer.read())'   "$MEIVA_HOME" "<ledger-cluster>" < receipt.json
```

The gateway or indexer delivers the recovered row on its next scan. The normal
ledger replay check handles repeats safely. The local fallback log has the
existing log retention window. If the host fails before staging or both local
storage and diagnostic logging are unavailable, reconcile against the provider
bill and the usage events that survived.

## Unpriced calls

A call the catalog cannot price (no price for the model at that time, or no rate for a cache-write class it
reported) is written to the ledger with its tokens and no cost, and emits one `llm_usage_unpriced` event per
call (one per harness turn). The Grafana rule `meiva-ops-llm-usage-unpriced` alerts per model with the cause
and the fix. After a release declares the rate, `services/upkeep/events_maintenance/ledger_repricing.py`
prices the row at its own `occurred_at` on the next hourly pass: no operator command, no rewrite of a
priced row (the select, the update and the table's `llm_usage_ledger_price_only` trigger each refuse
it). See [Cache-write pricing](../pricing/docs/cache-writes.md).

## Selecting usage scopes

LLM usage is attributed to agents. The independent
`meiva-being-a-long-running-agent` [usage script](../../../meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/references/usage.md)
selects IDs, windows, and birth ancestry. `base/telemetry/metrics/usage.py` owns its
read-only aggregation over the ledger, for any window or from its first report. Threshold
notifications leave convergence, preservation, and handoff to the agent.
