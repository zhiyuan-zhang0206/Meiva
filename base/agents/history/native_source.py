from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.messages.kwargs import MeivaMsgType, message_read_time, read_meiva_kwargs
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


_SINGLE_QUOTE_BLOCK = frozenset(
    {MeivaMsgType.INBOUND, MeivaMsgType.EXEC_OUTPUT, MeivaMsgType.SYSTEM_NOTE}
)
_SINGLE_NONQUOTE_BLOCK = frozenset(
    {MeivaMsgType.ATTACH, MeivaMsgType.COMPACT_SUMMARY, MeivaMsgType.COMPACT_REQUEST}
)


def native_source_stamps(messages: Sequence[BaseMessage]) -> dict[int, datetime | None]:
    sources = [(i, message) for i, message in enumerate(messages) if _has_source(message)]
    stamps = effective_native_stamps(
        [
            None if isinstance(message, SystemMessage) else message_read_time(message)
            for _, message in sources
        ]
    )
    return {i: stamp for (i, _), stamp in zip(sources, stamps, strict=True)}


def _has_source(message: BaseMessage) -> bool:
    if isinstance(message, SystemMessage | HumanMessage):
        return True
    kind = read_meiva_kwargs(message).get("meiva_msg_type")
    if kind in _SINGLE_QUOTE_BLOCK or kind in _SINGLE_NONQUOTE_BLOCK:
        return True
    return isinstance(message, AIMessage) and (
        bool(message.tool_calls)
        or any(block.text is not None for block in ai_source_blocks(message))
    )


def effective_native_stamps(values: Sequence[str | None]) -> list[datetime | None]:
    own = [moment_of(value) for value in values]
    stamps: list[datetime | None] = []
    previous: datetime | None = None
    for stamp in own:
        previous = stamp or previous
        stamps.append(previous)
    following: datetime | None = None
    for index in range(len(stamps) - 1, -1, -1):
        if own[index] is not None:
            following = own[index]
        elif stamps[index] is None:
            stamps[index] = following
    return stamps


def moment_of(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def has_native_quote_block(message: BaseMessage, block: int) -> bool:
    if isinstance(message, SystemMessage):
        return False
    kind = read_meiva_kwargs(message).get("meiva_msg_type")
    if kind in _SINGLE_QUOTE_BLOCK:
        return block == 0
    if kind in _SINGLE_NONQUOTE_BLOCK:
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
