# meiva:note
# Chunk assembly and final-message validation for a streamed model call. `assemble_final_message` folds the streamed `AIMessageChunk` list into one `AIMessage` via chunk addition, then runs the fail-fast validators: `sanitize_thinking_blocks` (DeepSeek thinking-delta drift repair) and `validate_stop_reason` (missing, truncated or unexpected terminal reason). A leaf of the llm node and of the compaction summary call; nothing here imports back into either.
from __future__ import annotations

from typing import TypeGuard

from langchain_core.messages import AIMessage, AIMessageChunk, message_chunk_to_message

from agent.graph.llm_errors import (
    LLMStreamCorruptedError,
    LLMStreamTruncatedError,
    LLMStreamUnexpectedStopReasonError,
)
from base.lm.catalog import ModelCatalog
from base.lm.reasoning import content_blocks
from base.log import logger


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# Repair thinking content blocks missing the `thinking` key (DeepSeek drift #167/168). DeepSeek's anthropic-compat endpoint intermittently drops the thinking_delta SSE event, so chunk accumulation yields a thinking block with a signature and no `thinking`. Filling `thinking=""` repairs the wire shape: the filled block round-trips the endpoint (probed 2026-07-25: 200; the key-missing shape 400s), and every downstream consumer (the streaming fan-out, the timeline renderer) already skips empty-thinking blocks. Repair in place replaces the old fail-fast guard: the drift is a permanent protocol quirk (~2% of deepseek turns), not a transient fault, so aborting the turn manufactured a doubled ~100K-token re-request plus a WARNING storm per hit. The sanitize INFO keeps the drift rate observable without counting as an operator alert. Scoped to `model_provider == 'anthropic'` (covers deepseek via ChatAnthropic) and to list content (thinking plus tool_use blocks); string content has no thinking blocks.
def sanitize_thinking_blocks(final_msg: AIMessage) -> None:
    if (final_msg.response_metadata or {}).get("model_provider") != "anthropic":  # pyright: ignore[reportUnknownMemberType]
        return
    content: object = final_msg.content  # pyright: ignore[reportUnknownMemberType]
    if not _is_object_list(content):
        return
    for i, block in enumerate(content_blocks(content)):
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        if not isinstance(block.get("thinking"), str):
            logger.info(
                "thinking block [{block_idx}] missing 'thinking' field "
                "(signature={signature!r}) — signature_delta without thinking_delta; "
                "filled empty thinking (round-trip verified), turn continues",
                event="thinking_block_sanitized",
                block_idx=i,
                signature=block.get("signature", "<missing>"),
            )
            block["thinking"] = ""


# meiva:note
# Terminal reasons that assert the model emitted a tool call: anthropic 'tool_use', openai 'tool_calls'. If one of these is set but no normalized tool_calls survived, the stream lost the tool block (bug 169): fail fast rather than idle.
_TOOL_CLAIMED_REASONS: frozenset[str] = frozenset({"tool_use", "tool_calls"})


# meiva:note
# Fail fast on a missing or abnormal terminal reason, normalized across providers by `classify_stop` (dispatched on response_metadata['model_provider']): anthropic `stop_reason`, openai/gemini `finish_reason` and their differing vocabularies map to one set of categories. A missing terminal reason is never silently treated as idle (`if not final_msg.tool_calls` would misjudge a lost final frame).
def validate_stop_reason(final_msg: AIMessage, *, catalog: ModelCatalog) -> None:
    from base.lm.compat.stop import StopCategory, classify_stop

    category, raw = classify_stop(final_msg, stops=catalog.stops)
    output_tokens = (final_msg.usage_metadata or {}).get("output_tokens", 0)  # pyright: ignore[reportUnknownMemberType]
    if category is StopCategory.CORRUPTED:
        raise LLMStreamCorruptedError(
            f"final_msg terminal reason missing (metadata keys={list((final_msg.response_metadata or {}).keys())!r}). "  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            "Provider stream protocol drift / final frame lost — cannot determine whether the model stopped "
            "normally; cannot fallthrough and let if-not-tool_calls misjudge as idle."
        )
    assert raw is not None  # noqa: S101
    if category is StopCategory.TRUNCATED:
        raise LLMStreamTruncatedError(
            f"LLM stream truncated by server: terminal reason={raw!r} output_tokens={output_tokens}. "
            "Server output budget exhausted — usually client-side max_tokens too small. Abort turn; retry re-streams.",
            stop_reason=raw,
            output_tokens=output_tokens,
        )
    if category is StopCategory.UNEXPECTED:
        raise LLMStreamUnexpectedStopReasonError(
            f"LLM stream ended with unexpected terminal reason={raw!r} output_tokens={output_tokens}. "
            "Non-idle abnormal state (safety / content_filter / recitation etc.) — fail-fast so the user sees it.",
            stop_reason=raw,
            output_tokens=output_tokens,
        )
    if raw in _TOOL_CLAIMED_REASONS and not final_msg.tool_calls:
        raise LLMStreamCorruptedError(
            f"terminal reason={raw!r} claims a tool call but final_msg.tool_calls is empty — "
            "server emitted a tool intent but no tool block in stream; cannot fallthrough as idle (bug 169)."
        )


# meiva:note
# Fold streamed chunks into one `AIMessage` (`message_chunk_to_message` narrows chat-model
# chunks whose langchain stubs remain untyped), repair missing `thinking` keys in place
# (#167/#168 DeepSeek drift), validate the terminal stop reason first so an SSE stream cut off
# before its terminal frame reports missing-terminal-reason diagnostics, and then reject a
# missing `usage_metadata` on an otherwise normal terminal frame as `LLMStreamCorruptedError`
# (agent 113 metadata-loss guard).
# meiva:link agent/graph/llm/docs/failures-and-retry.md
def assemble_final_message(chunks: list[AIMessageChunk], *, catalog: ModelCatalog) -> AIMessage:
    accumulated = chunks[0]
    for c in chunks[1:]:
        accumulated = accumulated + c
    final_msg = message_chunk_to_message(accumulated)
    assert isinstance(final_msg, AIMessage)  # noqa: S101
    sanitize_thinking_blocks(final_msg)
    validate_stop_reason(final_msg, catalog=catalog)
    if final_msg.usage_metadata is None:
        ak_keys = list(final_msg.additional_kwargs.keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        rm_keys = list((final_msg.response_metadata or {}).keys())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        content_len = len(final_msg.content) if isinstance(final_msg.content, str) else -1  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        tool_calls_len = len(final_msg.tool_calls)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        raise LLMStreamCorruptedError(
            f"final_msg.usage_metadata is None — abnormal state (additional_kwargs={ak_keys}, "
            f"response_metadata={rm_keys}, content_len={content_len}, tool_calls={tool_calls_len})"
        )
    return final_msg
