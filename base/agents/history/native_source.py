from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.messages.kwargs import MeivaMsgType, read_meiva_kwargs
from base.host.boundary.json_values import is_object_dict
from base.lm.content import message_addl_kwargs, message_content
from base.lm.reasoning import to_canonical_reasoning


@dataclass(frozen=True, slots=True)
class NativeMessageIdentity:
    message_id: str | None
    inbound_id: int | None


def native_message_identity(message: BaseMessage) -> NativeMessageIdentity:
    kwargs = read_meiva_kwargs(message)
    raw_inbound = kwargs.get("meiva_inbound_id")
    inbound = raw_inbound if type(raw_inbound) is int and raw_inbound > 0 else None
    message_id = message.id if message.id and not kwargs.get("meiva_ephemeral_message_id") else None
    return NativeMessageIdentity(message_id, inbound)


@dataclass(frozen=True, slots=True)
class AiSourceBlock:
    index: int
    text: str | None
    reasoning: bool = False


def ai_source_blocks(message: AIMessage) -> list[AiSourceBlock]:
    content = _fold_addl_reasoning_into_content(
        to_canonical_reasoning(message_content(message)), message_addl_kwargs(message)
    )
    if isinstance(content, str):
        return [AiSourceBlock(0, content)] if content else []
    blocks: list[AiSourceBlock] = []
    for pos, block in enumerate(content):
        if not is_object_dict(block) or block.get("type") not in ("text", "thinking"):
            continue
        reasoning = block.get("type") == "thinking"
        index = block.get("index", pos)
        text = block.get("thinking" if reasoning else "text")
        blocks.append(
            AiSourceBlock(
                index if isinstance(index, int) else pos,
                text if isinstance(text, str) and text else None,
                reasoning,
            )
        )
    return blocks


def has_native_quote_block(message: BaseMessage, block: int) -> bool:
    if isinstance(message, SystemMessage):
        return False
    kind = read_meiva_kwargs(message).get("meiva_msg_type")
    if kind in {MeivaMsgType.INBOUND, MeivaMsgType.EXEC_OUTPUT, MeivaMsgType.SYSTEM_NOTE}:
        return block == 0
    if kind in {MeivaMsgType.ATTACH, MeivaMsgType.COMPACT_SUMMARY, MeivaMsgType.COMPACT_REQUEST}:
        return False
    if isinstance(message, AIMessage):
        blocks = ai_source_blocks(message)
        if any(entry.index == block and entry.text is not None for entry in blocks):
            return True
        tool_start = len({entry.index for entry in blocks})
        return tool_start <= block < tool_start + len(message.tool_calls)
    return isinstance(message, HumanMessage) and block == 0


def _fold_addl_reasoning_into_content(
    content: str | list[object], additional_kwargs: dict[str, object]
) -> str | list[object]:
    from base.lm.reasoning import ContentBlock

    addl_reasoning = additional_kwargs.get("reasoning_content")
    if not (addl_reasoning and isinstance(addl_reasoning, str) and addl_reasoning.strip()):
        return content
    if isinstance(content, list):
        if any(is_object_dict(b) and b.get("type") == "thinking" for b in content):
            return content
        head: ContentBlock = {"type": "thinking", "thinking": addl_reasoning, "index": 0}
        return [head, *content]
    if isinstance(content, str):
        return [
            {"type": "thinking", "thinking": addl_reasoning, "index": 0},
            {"type": "text", "text": content, "index": 1},
        ]
    assert_never(content)
