# meiva:note
# The content-block shape an AIMessage(Chunk).content list carries.
#
# langchain types `BaseMessage.content` as `str | list[str | dict]` —
# the block dicts lose their shape at that boundary, so every consumer that walks
# the list re-derives the same `.get("type")` / `.get("text")` / `.get("thinking")`
# protocol by hand. `ContentBlock` names that shape once (all fields optional: a
# block carries only the keys its `type` implies) and `content_blocks` is the
# single seam that re-labels the list arm so those reads are typed.
#
# The typed accessors for the other loosely-typed members of a LangChain message
# (`message_content`, `message_addl_kwargs`, `message_response_metadata`) live here
# too: the same narrowing, one level up.

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypedDict, TypeGuard

from base.host.boundary.json_values import is_object_dict


class ContentBlock(TypedDict, total=False):
    """One provider content block of an AIMessage(Chunk).content list, as the LLM
    layer sees it after langchain parsing.

    Every field is NotRequired: a block carries only the keys its `type` implies
    — a text block has `text`; a thinking block has `thinking` and (on a
    signature_delta) `signature` but no `thinking`; an openai reasoning block has
    `summary`; a tool_use block has `id`/`name`/`input` or a streaming
    `partial_json`. `index` places the block in the content-block offset that
    aligns tool-call code items with the committed snapshot.
    """

    type: str
    text: str
    thinking: str
    signature: str
    index: int
    summary: list[dict[str, object]]
    id: str
    name: str
    input: dict[str, object]
    partial_json: str


# meiva:note
# The shape check behind `content_blocks`: every element is a string or a dict with string
# keys, which is all a provider content list ever holds.
def _is_block_list(content: Sequence[object]) -> TypeGuard[list[str | ContentBlock]]:
    return all(isinstance(block, str) or is_object_dict(block) for block in content)


# meiva:note
# Re-label the list arm of an AIMessage(Chunk).content as `str | ContentBlock`
# so a `for b in content_blocks(content): if isinstance(b, dict): b.get(...)`
# walk reads typed fields. Identity at runtime — langchain already hands us the
# str / dict blocks; this only checks their outer shape and attaches the
# `ContentBlock` type to the dicts.
def content_blocks(content: Sequence[object]) -> list[str | ContentBlock]:
    blocks = content if isinstance(content, list) else list(content)
    if not _is_block_list(blocks):
        raise TypeError("message content must be a list of strings and dict blocks")
    return blocks


# meiva:note
# Typed accessors for the loosely-typed LangChain message members.
#
# LangChain types `content` blocks and `additional_kwargs` / `response_metadata`
# as bare `dict` (Unknown keys), which propagates Unknown into every reader. These
# three accessors are the single narrowing points: each takes a Protocol view of the
# message (a parameter annotation widens the bare dicts), so the call sites in
# timeline / stop / reasoning-compat / labeler stay annotation-clean.


class _MessageContent(Protocol):
    @property
    def content(self) -> str | list[str | dict[str, object]]: ...


class _MessageAddlKwargs(Protocol):
    @property
    def additional_kwargs(self) -> dict[str, object]: ...


class _MessageResponseMetadata(Protocol):
    @property
    def response_metadata(self) -> dict[str, object]: ...


# meiva:note
# `msg.content` with its content-list block dicts retyped `dict[str, object]`.
def message_content(msg: _MessageContent) -> str | list[str | dict[str, object]]:
    return msg.content


# meiva:note
# `msg.additional_kwargs` as a plain `dict[str, object]` (langchain types it bare).
def message_addl_kwargs(msg: _MessageAddlKwargs) -> dict[str, object]:
    return msg.additional_kwargs


# meiva:note
# `msg.response_metadata` as a plain `dict[str, object]` (langchain types it bare).
def message_response_metadata(msg: _MessageResponseMetadata) -> dict[str, object]:
    return msg.response_metadata
