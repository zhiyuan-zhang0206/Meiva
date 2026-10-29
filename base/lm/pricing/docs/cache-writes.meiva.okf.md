---
type: doc
title: Cache-write pricing
description: Provider cache lifetimes and their usage-time price snapshots.
tags: [billing, llm-inference]
---

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

## Anthropic and history

The TTL breakdown supersedes the aggregate `cache_creation`; never add both.
Anthropic's aggregate-only responses use its default 5-minute lifetime.
Existing persisted usage-time costs and explicit unpriced records remain
historical evidence and are not recomputed from today's catalog.
