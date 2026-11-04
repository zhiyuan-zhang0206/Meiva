"""ChatOpenAI subclass that recovers the reasoning the base client drops.

Used by two providers that lack suitable community packages: Xiaomi MiMo (no
community package exists) and Zhipu GLM (`langchain-zhipuai` v0.0.1 is
unmaintained; `langchain_zhipu` v4.1.8 requires `langchain<0.3.0`).

Kimi uses its community package (`langchain-moonshot`), which captures reasoning
in `additional_kwargs["reasoning_content"]`. The streaming fan-out and timeline
handle that style, so this subclass is only needed for the remaining providers.

OpenAI-compatible providers that stream reasoning in the delta's
`reasoning_content` field (the DeepSeek convention). langchain-openai deliberately
does not extract non-standard delta fields (`reasoning_content`,
`reasoning_details`); its docstring says to use a provider-specific subclass.
This subclass overrides the single chunk-conversion seam to fold
`reasoning_content` into a canonical `{"type":"thinking", ...}` content block, so
the reasoning streams and renders through the exact same path as every other
provider (`base.lm.reasoning`, the streaming fan-out, the timeline).

Content is normalized to a list of blocks (thinking at index 0, text at index 1)
on every chunk so chunk accumulation merges by index cleanly — mixing a bare
string chunk with a block-list chunk would otherwise wrap the string into the
list out of order.

Round-trip safety: the `thinking` block is not a chat/completions *input* block,
so langchain-openai's `_format_message_content` strips it when the message is
sent back on the next turn (the provider never receives it). The stored message
can therefore carry the block for rendering without breaking the next request —
no display-projection copy needed (unlike the openai Responses `reasoning` item,
which the server requires echoed back).
"""

from __future__ import annotations

from collections.abc import Mapping

from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.content import ContentBlock, message_content


# meiva:note
# The first choice's `delta` of an openai-style stream chunk (the chunk itself, or wrapped
# under a `chunk` key), or an empty dict when the chunk carries no choice.
def _delta_of(chunk: Mapping[str, object]) -> dict[str, object]:
    choices = chunk.get("choices")
    if not choices:
        wrapped = chunk.get("chunk")
        choices = wrapped.get("choices") if is_object_dict(wrapped) else None
    if not is_object_list(choices) or not choices:
        return {}
    first = choices[0]
    delta = first.get("delta") if is_object_dict(first) else None
    return delta if is_object_dict(delta) else {}


class ReasoningContentChatModel(ChatOpenAI):
    """ChatOpenAI whose streaming surfaces a provider's `reasoning_content`."""

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, object],
        default_chunk_class: type,
        base_generation_info: dict[str, object] | None,
    ) -> ChatGenerationChunk | None:
        # langchain's ChatOpenAI base method is loosely typed (partially unknown).
        gen = super()._convert_chunk_to_generation_chunk(  # pyright: ignore[reportUnknownMemberType]
            chunk, default_chunk_class, base_generation_info
        )
        if gen is None or not isinstance(gen.message, AIMessageChunk):
            return gen
        reasoning = _delta_of(chunk).get("reasoning_content")
        msg = gen.message
        # base content is the text fragment (str) or "" for reasoning/tool-only
        # chunks. Re-emit uniformly as list-of-blocks so accumulation merges by
        # index: thinking@0 (reasoning precedes text for a reasoning model),
        # text@1, tool calls stay in tool_call_chunks.
        content = message_content(msg)
        text = content if isinstance(content, str) else ""
        # typed as the content union (not list[dict]) so the assignment below is
        # not rejected by list invariance against AIMessageChunk.content.
        blocks: list[str | dict[str, object]] = []
        if isinstance(reasoning, str) and reasoning:
            thinking: ContentBlock = {"type": "thinking", "thinking": reasoning, "index": 0}
            blocks.append(dict(thinking))
        if text:
            blocks.append({"type": "text", "text": text, "index": 1})
        msg.content = blocks
        return gen
