---
type: doc
title: LLM Interface
description: Meiva agent model invocation — one streaming request per try with bounded transient retry.
tags: []
---

# LLM Interface

## What it is

Meiva agent's LLM invocation layer — containing tool schema definition (`agent/llm/__init__.py`) and the LLM streaming reasoning node (`agent/graph/llm/node.py`). Follows a **single-tool** architecture: only one `execute_code` tool, all capabilities accessible via Python namespace.

## Core responsibilities

- **Tool Schema** (`agent/llm/__init__.py`): `@tool("execute_code")` defines `execute_code(code: str) -> str`, consumed by `bind_tools` via its name + docstring + arg types. Docstring explicitly states that `meiva` is no longer auto-imported — agent must explicitly `import meiva`
- **LLM Node** (`agent/graph/llm/node.py`): streaming LLM reasoning
  - Normal (has tool_call): `Command(goto="before_exec")`
  - After the final message is assembled, `agent/hooks/understanding_chunks.py:due_chunk_update` may enqueue an understanding chunk (provider `input_tokens` past the segment's previous cut by `MEIVA_UNDERSTANDING_CHUNK_TOKENS`) and returns the moved cut as a `compact` update merged into whichever command ends the turn; off unless `MEIVA_UNDERSTANDING_ENABLED` — see [[base/agents/history/hierarchy/docs/chunks.meiva.okf.md]]
  - No tool_call stop-turn / cancel: `Command(update={halted: True}, goto="after_exec")` (returns to claim to wait for next inbound, process does not exit)
- **Streaming** (`agent/graph/llm/_stream.py:consume_llm`): one `astream` request per try, bounded by TTFT, inter-chunk and total deadlines. A stall ends the turn without a non-streaming fallback or delayed retry. The deadlines are a value (`StreamBounds`, built by `stream_bounds(agent, catalog)` from the agent's pins, else its model's tuning); the arithmetic (how long the next chunk may take, whether the total ceiling is spent, the decode window) is `_deadlines.py`, a `strict` module of values, and `_stream.py` is the shell that reads the monotonic clock and awaits the model.
- **Failures, validation, and bounded retry**: provider exception classification (`base/lm/errors.py:classify_error`), the `FatalLLMStreamError` / `FatalProviderError` / `LLMRetryExhaustedError` taxonomy (`agent/graph/llm_errors.py`), chunk/stop-reason validation (`agent/graph/llm/_chunk.py`), and the pure `RetryPolicy` / `_backoff.py` transient retry shell (`agent/graph/llm/_retry.py`) are detailed in [[agent/graph/llm/docs/failures-and-retry.meiva.okf.md]].
- **typed content block reading**: `AIMessage(Chunk).content`'s list branch is flattened by langchain into `list[str | dict[str, Any]]`, `base/lm/reasoning.py:content_blocks()` re-tags it as `ContentBlock` (`TypedDict, total=False`), validators like `_validate_thinking_blocks` use it for block-type traversal instead of bare `.get()`; similarly, `additional_kwargs`'s `meiva_*` fields via `base/agents/messages/kwargs.py:read_meiva_kwargs()` obtain typed views (see [[messages.meiva.okf.md]])
- **Quiet completion**: a valid completed response with no tool call ends the turn (`halted=True`, `after_exec`), including empty text or reasoning-only content. The response and its usage remain in history. No automatic continuation or completion nudge is injected. Tool calls still route to execution; protocol errors, truncation, and cancellation retain their existing handling.
- **streaming output**: AIMessage text goes through two paths — real-time (RedisStreamHandler → ChatStart/ChatDelta → UI) and persistence (LangGraph state.messages → timeline endpoint)
- **cancel-race tests**: each of `llm`, `exec` and the compact hook has its own opt-in `tests/cancel_fixture.py`, and `graph/tests/cancel_fixture.py` combines them, bound beside the real consumers, rather than in the root plugin roster. Tests retain real node logic and restore subscription patches at teardown. The `llm` package's own tests do not need a cancel event: their context has no ops pool (no inbound queue), where `subscribe_interrupt` yields an event that never fires, and they stream from a scripted chat model (`agent/graph/llm/tests/llm_support.py:ScriptedModel`) through the real path.
- **interrupt handling**: `agent/graph/llm/_cancel.py:race_stream_vs_cancel` — `subscribe_interrupt` RAII, node entry listens for cancel/terminate inbound, `asyncio.wait` races streaming task; cancel discards partial generation of current turn, not entering history

## Key dependencies

- [[tool-calls.meiva.okf.md]] — LLM-generated code executed by exec node
- [[system-prompt.meiva.okf.md]] — dynamic assembly of system prompt
- [[agent/docs/state.meiva.okf.md]] — AIMessage stored into state.messages
- [[agent/db/docs/db.meiva.okf.md]] — Redis pub/sub wakeup for inbound messages

## Entry points

- `agent/llm/__init__.py:execute_code` — tool schema definition
- `agent/graph/llm/node.py:llm_node()` — LLM node (`_llm_node_impl` is the implementation body)
- `agent/graph/llm/_stream.py:consume_llm()` — single streaming entry
- `base/lm/errors.py:classify_error()` — cross-provider classification of provider exceptions (transient/permanent/unknown)
- `base/lm/factory.py:build_chat_model()` — ChatModel factory

## Notes

The stream handler records the first-to-last chunk window as decode time, separately
from request latency; an empty stream has no decode window. In unit tests the handler's publisher
is a sink, so those assertions do not prove event delivery or live provider speed.

- State type hint uses `from __future__ import annotations` + module attribute references to avoid capturing `BaseAgentState` alias and losing plugin dynamic fields
- Stream failure taxonomy, chunk validation, and bounded transient retries: [[agent/graph/llm/docs/failures-and-retry.meiva.okf.md]].
- Turn records, the failed-try protocol and per-turn persistence: [[turn-records.meiva.okf.md]].

- SSE-only model liveness: [[waiting.meiva.okf.md]].
