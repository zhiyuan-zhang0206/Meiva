# Cache-write pricing

Cache writes are subdivisions of total input, not an extra input charge.
Each quote subtracts reads and all writes from ordinary input, then prices
each partition once. A measured write without its declared rate stays unpriced.

## OpenAI Responses

The Meiva integration uses the default 30-minute lifetime, billed at 1.25x
ordinary input. It exposes no 12-hour cache option. `cache_write_30m` follows
the full-request context tier and actual Standard/Fast service.
LangChain maps `cache_write_tokens` to `cache_creation`, prefixed with the
service tier for legacy priority receipts. These counts are not Anthropic
5-minute writes. The model catalog API and picker expose the 30-minute rate.

Source: [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
checked 2026-10-29.

## One table says what each vendor reports

`VENDOR_CACHE_WRITES` (`base/lm/pricing/cache_writes.py`) lists the write classes each vendor's
usage can report. Three things read it, so they cannot drift apart: the usage mapping (an aggregate
creation count lands in the vendor's only class: OpenAI's 30-minute one, otherwise the 5-minute default,
including the harness path, which reads Codex's `cacheWriteInputTokens` the same way), the
`llm_usage_unpriced` alert text, and `test_price_completeness`, which fails the merge when a priced chat
model does not declare every class its vendor reports (a rate, or `0` for "reported, not billed"),
declares one it does not, or belongs to a vendor with no entry. A vendor that reports nothing has an
empty set; if it starts reporting writes anyway, the call is unpriced and the alert names the class.

## An unpriced call is repriced, not rewritten

A call whose class has no declared rate is stored with its tokens and no cost, and emits
`llm_usage_unpriced` (Grafana rule `meiva-ops-llm-usage-unpriced`: model, cause, missing classes, the
fix). When a release declares the rate, the hourly events-maintenance pass prices the row at its own
`occurred_at`. 2026-10-29: 237 OpenAI calls were filed as 5-minute writes for part of a day; OpenAI has
no such class, so the migration `llm_usage_ledger_price_only` refiled the stored counts under the 30-minute
class (`settle_writes`) and the pass priced them.

## Anthropic and history

The TTL breakdown supersedes the aggregate `cache_creation`; never add both.
Anthropic's aggregate-only responses use its default 5-minute lifetime.
Existing persisted usage-time costs remain historical evidence and are never recomputed from
today's catalog; only a row that never had a cost is priced.
