---
type: doc
title: Durable LLM Usage
description: '`base/lm/usage/` — canonical durable usage events and matching billing spans for completed LLM calls.'
tags:
- base
- library
- llm-inference
- billing
---

# Durable LLM Usage

`base/lm/usage/` turns completed LLM calls into durable `llm_usage` events and matching billing spans. It is the shared accounting boundary for both LangChain messages and provider-specific raw usage fields.

## Usage emitters

- `log_usage_from_message()` is the accounting path for completed LangChain messages; it records the `llm_usage` event, usage-time price snapshot (or `unpriced=1`), and its matching billing span.
- `log_usage_fields()` serves non-LangChain providers such as the Gemini embedding REST adapter and `decide()` (decision models, `tok_out=0`; see [[base/lm/docs/decision-models.meiva.okf.md]]). `for_agent_id` explicitly attributes a daemon-generated event to its target agent; `source` labels the shared caller.
- `usage_kind` distinguishes agent, chat, batch, and embedding consumption. `source` is an optional payload field for shared text callers (`web.fetch`, `understand`, and `understand.media`) and is kept distinct from the transport provenance column.
- `log_usage_from_message(..., stamp_message=True)` (the main-conversation `log_llm_usage`) also writes the event's figures to `msg.additional_kwargs["meiva_usage"]`: one `quote`, one dict, two destinations.
- Gemini chat calls use ordinary implicit-cache accounting; `cache_read` is the provider-reported cached input.
- Cache fields are absent when a provider supplies no cache metering (including current decision and embedding calls). A reported `cache_read=0` is a measured miss; absence is N/A, never an inferred miss. Usage, price, latency and billing still record every completed call. Cache rates use only input from the calls reporting cache reads in both numerator and denominator.
- Previously emitted synthetic zeroes remain historical observations; they are not rewritten as unknown. Inspector projections preserve the legacy denominator for old rows without the new measured-input counter.

## Direct callers

`build_chat_model(...)` meters its own calls: `base/lm/usage/metering.py` attaches one LangChain callback handler to the built model, and every completed provider call (`invoke`, `ainvoke`, `stream`, `astream`, `batch`) writes the same `llm_usage` event `log_usage_from_message` writes, with `usage_kind="direct"`. Before this, a worker or script that built a model and called it directly spent money with no row at all (task #5327).

Exactly one layer owns a call's row. The factory owns it by default. A caller that emits the row itself — with its own `usage_kind`, `source` or agent id — builds with `meter=False`, or strips the meter from an injected build with `unmeter(model)`. The meter also stamps `msg.additional_kwargs["meiva_usage"]`, and `log_usage_from_message` refuses a message that already carries the stamp, so a caller that logs the same response cannot produce a second row.

`attribution` says how a row's agent identity was established: `caller` (the emitter supplied `for_agent_id`), `process` (the identity bound at telemetry startup), or `unattributed` (no identity was available, `agent_id` is NULL). A meter that cannot account for a call reports one `llm_usage_metering_failed` event and a rate-limited warning; it never fails the model call.

## Notes

- Usage events keep price snapshots at the time of use so cost accounting remains stable when catalog rates later change.
- Cache-write counts (`cache_write_5m`, `cache_write_1h`) and declared rate snapshots (`price_write_5m`, `price_write_1h`) preserve TTL-specific creation costs. Writes are included in `in_total`, so they are deducted from ordinary input, priced once, and also recorded as additive billing-span attributes. Actual served Standard/Fast identity selects all rates together. Existing persisted cost snapshots are not rewritten.
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

- Key deps: [[lm.meiva.okf.md]] (provider-layer overview) and [[pricing.meiva.okf.md]] (price selection).

## Selecting usage scopes

LLM usage is attributed to agents, not Fleet task records. The independent
`meiva-being-a-long-running-agent` [usage script](../../../../meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/references/usage.md)
selects IDs, windows, and birth ancestry. `base/telemetry/metrics/usage.py` owns its
read-only aggregation over retained events or lifetime ledger + tail. Threshold
notifications leave convergence, preservation, and handoff to the agent.
