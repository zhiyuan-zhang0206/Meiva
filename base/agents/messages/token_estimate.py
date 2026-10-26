# meiva:note
# Native whitespace scanning preserves CJK and other character weights for all Unicode characters and large text runs.
# meiva:link base/agents/messages/docs/token-estimation.meiva.okf.md
from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, TypeGuard

from langchain_core.messages import AIMessage, BaseMessage

from base.lm.reasoning import content_blocks

CJK_TOKENS_PER_CHAR = 0.95

OTHER_TOKENS_PER_CHAR = 0.36


NON_AI_MESSAGE_OVERHEAD_TOKENS = 15.0
AI_MESSAGE_OVERHEAD_TOKENS = 2.0

_CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uff00-\uffef]")


def _is_object_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def estimate_text_tokens(text: str) -> float:
    cjk = len(_CJK.findall(text))

    other = sum(map(len, text.split())) - cjk
    return CJK_TOKENS_PER_CHAR * cjk + OTHER_TOKENS_PER_CHAR * other


def estimate_message_tokens(msg: BaseMessage) -> float:
    if isinstance(msg, AIMessage):
        parts = ai_message_texts(msg)
        return AI_MESSAGE_OVERHEAD_TOKENS + sum(estimate_text_tokens(t) for t in parts.values())
    return NON_AI_MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(content_text(msg.content))


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if _is_object_list(content):
        parts: list[str] = []
        for b in content_blocks(content):
            if isinstance(b, dict):
                if isinstance(text := b.get("text"), str):
                    parts.append(text)
                elif isinstance(thinking := b.get("thinking"), str):
                    parts.append(thinking)
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return str(content)


def ai_message_texts(msg: AIMessage) -> dict[str, str]:
    out = {"reasoning": "", "output": "", "tool_call": ""}
    content: Any = msg.content
    if isinstance(content, str):
        out["output"] += content
    elif _is_object_list(content):
        for b in content_blocks(content):
            if not isinstance(b, dict):
                continue
            if isinstance(thinking := b.get("thinking"), str):
                out["reasoning"] += thinking
            elif b.get("type") == "text" and isinstance(text := b.get("text"), str):
                out["output"] += text
    for tc in msg.tool_calls:
        if isinstance(code := tc["args"].get("code"), str):
            out["tool_call"] += code
    return out


# meiva:note
# Largest-remainder apportionment conserves total tokens exactly across weighted parts.
def apportion(weights: Sequence[int], total: int) -> list[int]:
    if not weights:
        return []
    basis = list(weights) if sum(weights) > 0 else [1] * len(weights)
    denom = sum(basis)
    shares = [total * w // denom for w in basis]
    by_remainder = sorted(range(len(basis)), key=lambda i: -(total * basis[i] % denom))
    for i in by_remainder[: total - sum(shares)]:
        shares[i] += 1
    return shares
