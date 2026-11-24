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

`attribution` says how a row's agent identity was established: `caller` (the emitter supplied `for_agent_id`), `process` (the identity bound at telemetry startup), or `unattributed` (no identity was available, `agent_id` is NULL). Each exhausted ledger write or conflicting replay emits one `llm_usage_ledger_write_failed` warning before propagating as a failed step. The OTLP logs lane sends the full event to Loki. Count exhausted writes and replay conflicts in Grafana with LogQL `sum(count_over_time({event_name="llm_usage_ledger_write_failed"}[5m]))` and inspect `reason`, `error_type`, and `call_id` in the event payload. It is a log-derived count under the explicit metric allowlist.

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

Cluster process boot supplies the database and machine identity. The usage path
commits before the caller advances, using a separate transaction where the
caller has no checkpoint transaction to share. The first committed report owns
its time, machine and price snapshot. A replay of matching usage acknowledges
that row. Conflicting provider facts raise. Meiva receipts use the provider and
its request or response ID from `response_metadata`. The graph assigns its own
message ID separately. Direct callbacks use the LangChain run ID and input
index. A harness turn uses the provider session, the process generation persisted
in the session registry, the local turn ID, and the step index. The generation
advances when the same session resumes in a new process.

The writer retries a transient database failure once with the same frozen
report. An exhausted commit still emits `llm_usage` with the same ID, original
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
differences. Its commands are retired. Reports have no retention expiry; the
Loki `llm_usage` stream is the operational copy and keeps seven days.

## Selecting usage scopes

LLM usage is attributed to agents. The independent
`meiva-being-a-long-running-agent` [usage script](../../../meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/references/usage.md)
selects IDs, windows, and birth ancestry. `base/telemetry/metrics/usage.py` owns its
read-only aggregation over the ledger, for any window or from its first report. Threshold
notifications leave convergence, preservation, and handoff to the agent.
