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

from collections.abc import Callable, Sequence
from typing import Protocol

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatResult

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.compat.provider_sdks.anthropic_sdk import anthropic

type ImageDownscaler = Callable[[str, int], str]


# meiva:note
# Anthropic signatures are base64-encoded protobuf tokens without hyphens (>= 50 chars).
# DeepSeek and synthetic fixtures produce UUID4 strings (with hyphens) or short tokens.
def is_foreign_thinking_signature(signature: object) -> bool:
    if not isinstance(signature, str):
        return True
    return "-" in signature or len(signature) < 50


# meiva:note
# Redacted thinking tokens from non-Anthropic providers also carry foreign placeholders.
def _is_foreign_redacted_data(data: object) -> bool:
    if not isinstance(data, str):
        return True
    return "-" in data or len(data) < 50


# meiva:note
# Strip foreign thinking blocks from an AIMessage, replacing with an empty text block if empty.
def _sanitize_ai_content(content: Sequence[object]) -> list[object]:
    new_content: list[object] = []
    for block in content:
        if is_object_dict(block):
            b_type = block.get("type")
            if b_type == "thinking" and is_foreign_thinking_signature(block.get("signature")):
                continue
            if b_type == "redacted_thinking" and _is_foreign_redacted_data(block.get("data")):
                continue
        new_content.append(block)
    if not new_content:
        return [{"type": "text", "text": ""}]
    return new_content


# meiva:note
# Count image_url blocks across messages to determine whether multi-image limits apply.
def _count_image_blocks(messages: Sequence[BaseMessage]) -> int:
    count = 0
    for msg in messages:
        content = msg.content
        if isinstance(content, list):
            for block in content:
                if is_object_dict(block) and block.get("type") == "image_url":
                    count += 1
    return count


# meiva:note
# Downscale image_url blocks exceeding max_multi_image_px via the injected downscaler.
def _downscale_images_in_content(
    content: Sequence[object],
    image_downscaler: ImageDownscaler,
    max_multi_image_px: int,
) -> tuple[list[object], bool]:
    changed = False
    new_blocks: list[object] = []
    for block in content:
        if is_object_dict(block) and block.get("type") == "image_url":
            img_obj = block.get("image_url")
            if is_object_dict(img_obj):
                url_val = img_obj.get("url")
                if isinstance(url_val, str):
                    new_url = image_downscaler(url_val, max_multi_image_px)
                    if new_url != url_val:
                        new_img_dict: dict[str, object] = dict(img_obj)
                        new_img_dict["url"] = new_url
                        new_block: dict[str, object] = dict(block)
                        new_block["image_url"] = new_img_dict
                        new_blocks.append(new_block)
                        changed = True
                        continue
        new_blocks.append(block)
    return new_blocks, changed


# meiva:note
# Sanitize message history for Anthropic Claude: strip foreign thinking blocks and downscale images.
def sanitize_anthropic_messages(
    messages: Sequence[BaseMessage],
    *,
    image_downscaler: ImageDownscaler | None = None,
    max_multi_image_px: int = 3000,
) -> list[BaseMessage]:
    multi_image = _count_image_blocks(messages) > 1
    sanitized: list[BaseMessage] = []
    changed_any = False

    for msg in messages:
        content = msg.content
        msg_changed = False
        if isinstance(msg, AIMessage) and isinstance(content, list):
            new_content = _sanitize_ai_content(content)
            if new_content != content:
                content = new_content
                msg_changed = True

        if multi_image and image_downscaler is not None and isinstance(content, list):
            content, images_changed = _downscale_images_in_content(
                content, image_downscaler, max_multi_image_px
            )
            msg_changed = msg_changed or images_changed

        if msg_changed:
            changed_any = True
            sanitized.append(msg.model_copy(update={"content": content}))
        else:
            sanitized.append(msg)

    return sanitized if changed_any else list(messages)


# meiva:note
# The part of an Anthropic response `_format_output` reads: its `usage`, whose `speed` the SDK model
# declares only as an extra field.
class _ResponseWithUsage(Protocol):
    @property
    def usage(self) -> object: ...


class ThinkingTokensChatAnthropic(ChatAnthropic):
    """ChatAnthropic that retains `thinking_tokens` in `usage_metadata`.

    Overrides the single chunk-conversion seam so downstream consumers
    (`agent.llm.usage.log_llm_usage`, `agent.graph.llm.node._finalize_turn_observability`,
    `base.agents.history.timeline`, `gateway.inspect.router`) all see a non-zero
    `reasoning` count — no other file needs to change.
    """

    def __init__(
        self,
        *args: object,
        image_downscaler: ImageDownscaler | None = None,
        **kwargs: object,
    ) -> None:
        # meiva:note
        # Retain the image downscaler before passing arguments to ChatAnthropic.__init__.
        super().__init__(*args, **kwargs)  # pyright: ignore[reportAny]
        self._image_downscaler = image_downscaler

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        # meiva:note
        # Strip foreign thinking signatures and resize multi-image requests before Anthropic payload formatting.
        if self.model.startswith("claude-"):
            messages = self._convert_input(input_).to_messages()
            input_ = sanitize_anthropic_messages(
                messages,
                image_downscaler=self._image_downscaler,
            )
        return super()._get_request_payload(input_, stop=stop, **kwargs)  # pyright: ignore[reportAny, reportUnknownMemberType, reportUnknownVariableType]

    def _format_output(
        self,
        data: _ResponseWithUsage,
        *,
        generation_info: dict[str, object] | None = None,
        **kwargs: object,
    ) -> ChatResult:
        result = super()._format_output(data, generation_info=generation_info, **kwargs)
        speed = getattr(data.usage, "speed", None)
        if speed is not None:
            for generation in result.generations:
                generation.message.response_metadata["speed"] = speed
        return result

    def _make_message_chunk_from_anthropic_event(
        self,
        event: anthropic.types.RawMessageStreamEvent,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool = False,
        block_start_event: anthropic.types.RawMessageStreamEvent | None = None,
    ) -> tuple[AIMessageChunk | None, anthropic.types.RawMessageStreamEvent | None]:
        msg, next_block_start = super()._make_message_chunk_from_anthropic_event(
            event,
            stream_usage=stream_usage,
            coerce_content_to_string=coerce_content_to_string,
            block_start_event=block_start_event,
        )
        return retain_usage_details(msg, event), next_block_start


def retain_usage_details(msg: AIMessageChunk | None, event: object) -> AIMessageChunk | None:
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
        message = getattr(event, "message", None)
        speed = getattr(getattr(message, "usage", None), "speed", None)
        if speed is not None:
            msg.response_metadata["speed"] = speed
    # meiva:note
    # The `message_delta` event carries the final usage including
    # output_tokens_details.thinking_tokens — which the upstream
    # _create_usage_metadata drops. Supplement the chunk's usage_metadata.
    elif kind == "message_delta" and msg.usage_metadata is not None:
        usage = object_of(getattr, event, "usage", None)
        output_details = object_of(getattr, usage, "output_tokens_details", None)
        if output_details is not None:
            reported = object_of(getattr, output_details, "thinking_tokens", 0)
            thinking_tokens = reported if isinstance(reported, int) else 0
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
