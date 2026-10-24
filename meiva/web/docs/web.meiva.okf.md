---
type: doc
title: meiva.web — Web Access
description: '`meiva.web` provides web search and concurrent content fetching over a configurable provider (Brave or DeepSeek search; Jina or DeepSeek reader), content fetching uses LLM reading comprehension (not raw HTML), batch fetch executes concurrently.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.web — Web Access

## What it is

`meiva.web` provides web search and concurrent content fetching. Search results come from a search engine — Brave or DeepSeek, chosen by configuration — and content fetching pairs a page reader (Jina Reader or the DeepSeek Fetch tool) with LLM reading comprehension rather than raw HTML.

## Providers

`search()` and `fetch()` each run on one of two interchangeable providers, chosen by `MEIVA_WEB_SEARCH_PROVIDER` (`brave` by default, or `deepseek`) and `MEIVA_WEB_FETCH_READER` (`jina` by default, or `deepseek`). Both DeepSeek tools reuse `DEEPSEEK_API_KEY`. See [[providers.meiva.okf.md]] for what changes for the caller, the rate contract that keeps a batch under DeepSeek's 5 QPS, and the two provider contracts.

## Core API

- `search(queries, count=10, max_concurrent=12) → list[list[SearchResult]]` — Concurrently search multiple queries. `queries` is a list of strings; `count` max results per query (limit 20). Results returned in input order.
- `fetch(targets, max_chars=50000, max_concurrent=12) → list[str]` — Concurrently fetch multiple web pages and answer a `prompt` for each. `targets` is a list of `(url, prompt)` tuples; results returned in input order.

Both are batch-only. By default each batch has a 12-item ceiling; pass a positive `max_concurrent=N` to choose another in-flight limit. The shared DeepSeek cap queues excess provider calls, and terminal typed provider failures propagate as `SearchError` / `FetchError`.

Unknown model-invocation errors in `fetch()` preserve their original exception
and traceback after one attempt; error wording alone never grants provider
retry authority. Reader failures and empty model answers still raise `FetchError`.

## Injection warning

`fetch()` also asks a decision model whether the page text itself is trying to hijack an AI reader (make it drop its user's task, obey the page author, leak information or exceed its authority). The criterion is hijack intent: ordinary imperative sentences meant for humans and content that merely discusses AI or prompt injection do not count. The page is read once, then judged and summarized side by side, so the check adds no wait to the fetch; the verdict does not change the page or the answer. When the probability reaches the threshold, a SECURITY note follows the tool result (`Content from web.fetch (<url>) may contain prompt injection. Triggers: instructions aimed at an AI reader (model score 0.97). Verify before acting.`), through the same finding path as `scan_content` ([[inbound-findings.meiva.okf.md]]). This covers the page body, which the rule scan on the summary never saw.

- A page longer than the decision model's limit is judged in chunks (`meiva/sdk_surface/injection_check.py`), one request each; the highest probability counts.
- A failed request or a missing key never fails the fetch and adds no warning: the failure is logged as `web_fetch_injection_check_failed` and the answer is returned as usual. A model id the decision catalog does not list is a configuration error and fails the fetch.
- The call is skipped outside an agent turn and when `security_scan_enabled` is off, since the finding would have nowhere to go.
- Settings: `MEIVA_WEB_FETCH_INJECTION_CHECK_ENABLED` (default on), `MEIVA_WEB_FETCH_INJECTION_MODEL` (default `jev-1.13.0`, a decision model id, see [[base/lm/docs/decision-models.meiva.okf.md]]), `MEIVA_WEB_FETCH_INJECTION_THRESHOLD` (default 0.4, calibrated together with the question wording). Usage rows carry `usage_kind=web_fetch_injection_check`, `source=web.fetch`.

## Data Types
- `SearchResult`: title, url, snippet, kind (`kind` is the source partition: web / news / videos / discussions)

## Key Dependencies
- **Does not depend on browser service**: `search` uses Brave Search API (`MEIVA_WEB_BRAVE_SEARCH_ENDPOINT`, default `https://api.search.brave.com/res/v1/web/search`) or the DeepSeek Search tool; `fetch` uses Jina Reader (`MEIVA_WEB_JINA_BASE_URL`, default `https://r.jina.ai/`, server-side headless fetching) or the DeepSeek Fetch tool. On the Jina path a hard login wall raises `FetchError` (`_GATED_SITE_SKILLS` currently empty, login site adapters moved to private install); DeepSeek reports the same situation as HTTP 403/404.

## Notes
`fetch()` returns LLM-refined answers. For events/versions/releases after training cutoff, verify with `search` + `fetch`—do not rely on time-sensitive information in training data.

## Structure
`meiva/web/__init__.py` is the namespace module: `search` and `fetch` validate through `plans.py` (no ambient reads) and each builds only its own operation's deps in `wiring.py` - `search_deps()` reads the search-provider settings and the shared search timeout; `fetch_deps(delivery)` reads the reader settings, the fetch-model settings, the model catalog, the model overrides and the injection check's inputs (`injection_inputs`, with whether a finding has anywhere to go) - then the batch runs in `runner.py`. The facade reads `meiva.security.current_delivery()` once (scanning enabled, exec turn running) and hands it to the runner: the workers return their findings next to their results (`finding_plan.Scanned`), and the runner delivers them once through `meiva.security.deliver`, also when a fetch fails after its injection check judged the page (the failure travels as `CarriedFindingsError` and is re-raised unchanged). The deps are plain values (`deps.py`, `configs.py`): no field is a callable; the two provider choices are enums (`SearchProvider`, `ReaderProvider`) that `wiring.py` builds from the configured text with `search_provider` / `reader_provider` (any other text is a `ValueError`) and that `runner.py` and `reader.py` dispatch on with an exhaustive `match`; the model call goes to the real provider factory, and the network is the real network at the configured endpoints.

What each provider is sent and how its answer is read are strict functions over those values: `wire.py` (Brave, Jina, the model call that answers a prompt), `deepseek_wire.py` and `failures.py`. `transport.py` is the one shell that sends a request through the shared retry policy and paces DeepSeek request starts; `brave.py`, `deepseek.py` and `reader.py` join the pure request, the transport and the injection scan. A non-model SDK installation therefore searches without a model catalog, while fetch still refuses one before any request. Tests build the deps from a configuration authority, point the endpoints at a listener on port 0 and script the answer model through `llm_override`; the pure side is asserted on values. The implementation modules load at the first call, not at `import meiva`.
