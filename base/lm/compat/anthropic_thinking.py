"""ChatAnthropic subclass that surfaces thinking_tokens in usage_metadata.

langchain-anthropic's `_create_usage_metadata` drops `output_tokens_details`
from the Anthropic SDK `Usage` model — even though the model has
`output_tokens_details.thinking_tokens` (the reasoning token count, present for
every thinking-enabled call). Without it, `usage_metadata.output_token_details`
is always empty for all `ChatAnthropic`-routed models (claude-*, deepseek-*), so
`reasoning` stays 0 in observability.

`ThinkingTokensChatAnthropic` overrides the stream-chunk seam
(`_make_message_chunk_from_anthropic_event`) to capture `thinking_tokens` from
the raw `message_delta` event BEFORE `_create_usage_metadata` drops it, and
supplements the chunk's `usage_metadata.output_token_details.reasoning`.

This is the Anthropic-side mirror of `base/lm/compat/openai_reasoning.py`
(`ReasoningContentChatModel` for ChatOpenAI).
"""

from __future__ import annotations

from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatResult


class ThinkingTokensChatAnthropic(ChatAnthropic):
    """ChatAnthropic that retains `thinking_tokens` in `usage_metadata`.

    Overrides the single chunk-conversion seam so downstream consumers
    (`agent.llm.usage.log_llm_usage`, `agent.graph.llm.node._finalize_turn_observability`,
    `base.agents.history.timeline`, `gateway.inspect.router`) all see a non-zero
    `reasoning` count — no other file needs to change.
    """

    def _format_output(
        self, data: Any, *, generation_info: dict[str, Any] | None = None, **kwargs: Any
    ) -> ChatResult:
        result = super()._format_output(data, generation_info=generation_info, **kwargs)
        speed = getattr(data.usage, "speed", None)
        if speed is not None:
            for generation in result.generations:
                generation.message.response_metadata["speed"] = speed
        return result

    def _make_message_chunk_from_anthropic_event(
        self,
        event: Any,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool = False,
        block_start_event: Any = None,
    ) -> tuple[AIMessageChunk | None, Any]:
        msg, block_start_event = super()._make_message_chunk_from_anthropic_event(
            event,
            stream_usage=stream_usage,
            coerce_content_to_string=coerce_content_to_string,
            block_start_event=block_start_event,
        )
        return retain_usage_details(msg, event), block_start_event


def retain_usage_details(msg: AIMessageChunk | None, event: Any) -> AIMessageChunk | None:
    # meiva:note
    # The chunk the SDK's conversion built is the input here, so a caller (and a
    # test) supplies it directly; this puts back the two details that conversion
    # drops: the service `speed` on a `message_start` event, and
    # `output_tokens_details.thinking_tokens` on a `message_delta` event, which
    # lands as `output_token_details.reasoning`.
    if msg is None:
        return msg
    kind = getattr(event, "type", None)
    # meiva:note
    # Emit once: LangChain concatenates repeated string metadata while
    # combining chunks. The start event carries the actual service speed.
    if kind == "message_start":
        speed = getattr(event.message.usage, "speed", None)
        if speed is not None:
            msg.response_metadata["speed"] = speed
    # meiva:note
    # The `message_delta` event carries the final usage including
    # output_tokens_details.thinking_tokens — which the upstream
    # _create_usage_metadata drops. Supplement the chunk's usage_metadata.
    elif kind == "message_delta" and msg.usage_metadata is not None:
        output_details = getattr(event.usage, "output_tokens_details", None)
        if output_details is not None:
            thinking_tokens: int = getattr(output_details, "thinking_tokens", 0) or 0
            if thinking_tokens > 0:
                existing = msg.usage_metadata.get("output_token_details") or {}
                if "reasoning" not in existing and "reasoning_tokens" not in existing:
                    # meiva:note
                    # Construct a fresh OutputTokenDetails dict (TypedDict,
                    # total=False) so the type checker is satisfied.
                    msg.usage_metadata["output_token_details"] = {
                        **existing,
                        "reasoning": thinking_tokens,
                    }
    return msg
