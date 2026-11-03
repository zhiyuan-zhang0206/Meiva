---
type: doc
title: LLM Stream Failures, Validation, and Retry Policy
description: Provider error classification, chunk and stop-reason validation, bounded transient retries, and the FatalLLMStreamError settlement boundary.
tags: [agent, graph]
---

# LLM stream failures, validation, and retry policy

## Provider classification and fatal taxonomy

- **Provider exception classification** (`base/lm/errors.py:classify_error`): official LangChain `ModelError.is_retryable` and actual SDK types distinguish trusted `TRANSIENT` failures from deterministic `PERMANENT` rejections. `UNKNOWN` errors propagate once unchanged; unrelated attributes or arbitrary causes cannot grant retry authority. Raw HTTPX transport is normalized only at the model invoke/iterator await, excluding output callbacks and unrelated services. `emit_provider_error` records the shared classification and billing signal; `agent/graph/llm_errors.py:classify_and_log_provider_error` applies the configured fatal-provider policy (`FatalProviderError`).
- **Turn-abort settlement hierarchy** (`agent/graph/llm_errors.py`): permanent rejections (`FatalProviderError`), stream stalls (`LLMStreamStallTimeoutError`), retry-budget expiry (`LLMRetryBudgetExceededError`), protocol aborts (`LLMStreamCorruptedError`, `LLMStreamUnexpectedStopReasonError`, `LLMStreamTruncatedError`), and exhausted transient provider retries wrapped at the hosted invocation boundary (`LLMRetryExhaustedError` via `retry_exhausted_error`) end the current turn with diagnostics. All stream/retry-exhaustion aborts subclass `FatalLLMStreamError`, so they ride the host's settled turn abort (`halted=True` + the user-visible `Error` event) without opening the permanent heartbeat circuit breaker (`llm_node` itself still re-raises the original retryable provider error when its retry cap is exhausted). Permanent rejections (`FatalProviderError`) open the circuit breaker to block background wakes; fresh chat or an explicit control can retry after the cause is fixed. Context overflow requires explicit compaction rather than a model-free history wipe.

## Chunk assembly and stop-reason validation

`agent/graph/llm/_chunk.py:assemble_final_message` folds streamed `AIMessageChunk` frames into one `AIMessage` and validates protocol completeness in order:

1. `sanitize_thinking_blocks` repairs the established DeepSeek missing-`thinking`-field drift in place (`thinking=""`).
2. `validate_stop_reason` checks missing (`LLMStreamCorruptedError`), truncated (`LLMStreamTruncatedError`), or unexpected (`LLMStreamUnexpectedStopReasonError`) terminal reasons, and rejects a tool-claiming stop reason whose `final_msg.tool_calls` is empty (`LLMStreamCorruptedError`). Running `validate_stop_reason` before checking `usage_metadata` ensures an SSE stream cut off at EOF before its terminal frame surfaces missing-stop-reason diagnostics rather than a secondary metadata check.
3. Missing `final_msg.usage_metadata` on an otherwise normal terminal frame raises `LLMStreamCorruptedError` with diagnostic metadata keys, content length, and tool-call count (never bare `AssertionError`).

Protocol validation failures raise once without automatic re-streaming and keep the failed turn visible.

## Bounded transient retry shell

The retry loop in `agent/graph/llm/node.py:llm_node` is a thin shell over values. `agent/graph/llm/_retry.py:retry_policy` reads the agent's lm config once, with the model's try cap resolved, into a frozen `RetryPolicy`; `agent/graph/llm/_backoff.py` (a `strict` module) answers from values only: the next wait (`backoff_wait`: policy, tries failed, the budget the failed try recorded, a jitter roll), whether the budget is spent (`budget_spent`), and which limit a failed try hit (`assess_failure`). The shell reads the wall clock, draws the jitter from the generator `build_graph` handed it, sleeps, and logs. Tests assert the plan with values (`tests/test_backoff.py`, `test_deadlines.py`) and run the loop through a scripted chat model with small configured intervals.

The node retries trusted 429/transport/server failures under the per-model attempt cap and total wall-clock budget, with bounded backoff. Provider SDK construction keeps its existing finite retries. `llm_retry` records the final node retry duration. No retry re-enters the graph or repeats code execution.
