"""Provider-agnostic reasoning normalization — the seam above LangChain,
below the agent kernel.

Each provider carries its reasoning in a different content-block shape:

- anthropic (claude) / gemini: `{"type":"thinking", "thinking":"<text>"}`
- openai Responses API: `{"type":"reasoning", "summary":[{"type":"summary_text",
  "text":"<text>"}]}` — the visible text is nested in summary[].text

`to_canonical_reasoning` maps any of these to the single shape the timeline,
the streaming fan-out, and the eval extractor already speak — the `thinking`
block — so those consumers never branch on provider. Every other block (text,
tool_use, function_call, ...) passes through untouched.

This is a DISPLAY projection, not a rewrite of stored state. The committed
AIMessage must keep its provider-native blocks: openai 400s on the next turn
if its `reasoning` item is not echoed back verbatim alongside the matching
`function_call` (the same round-trip contract anthropic has for thinking +
signature). So callers normalize a *copy* for rendering and leave the stored
message native.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import UsageMetadata

from base.lm.content import ContentBlock, content_blocks


# meiva:note
# The text of one openai `summary_text` part, or "" when the part carries none.
def _text_of(part: dict[str, object]) -> str:
    text = part.get("text")
    return text if isinstance(text, str) else ""


# meiva:note
# A provider's token count as an int: nothing (None, 0, empty) counts as zero, a number or numeric
# text is converted (non-numeric text raises `ValueError`), any other type is refused naming `key`.
def _token_count(value: object, key: str) -> int:
    if not value:
        return 0
    if isinstance(value, int | float | str):
        return int(value)
    raise TypeError(f"token count {key!r} must be a number, got {type(value).__name__}")


def _canonical_block(block: ContentBlock) -> ContentBlock:
    """Map one openai Responses `reasoning` block to a `thinking` block, or
    return `block` unchanged.

    The visible text is the concatenation of summary[].text. `index` is
    preserved so the content-block offset that places tool-call code items is
    unchanged. A reasoning block with no summary text (the boundary/initial
    chunk) maps to an empty thinking block, which consumers skip exactly like
    an anthropic signature-only block.
    """
    if block.get("type") != "reasoning":
        return block
    text = "".join(_text_of(part) for part in (block.get("summary") or []))
    out: ContentBlock = {"type": "thinking", "thinking": text}
    if "index" in block:
        out["index"] = block["index"]
    return out


# meiva:note
# Normalize provider-native reasoning blocks to the canonical `thinking`
# shape.
#
# `content` is an AIMessage(Chunk).content: a string (returned unchanged) or
# a list of blocks (returns a new list — reasoning blocks normalized, every
# other element, dict or str, passed through unchanged). The input is never
# mutated; the stored message stays provider-native.
def to_canonical_reasoning(content: str | Sequence[object]) -> str | list[object]:
    if not isinstance(content, list):
        return content if isinstance(content, str) else list(content)
    return [_canonical_block(b) if isinstance(b, dict) else b for b in content_blocks(content)]


# meiva:note
# Extract reasoning token count from usage_metadata, with content fallback.
#
# Primary: read `output_token_details.reasoning` (Anthropic/DeepSeek via
# ThinkingTokensChatAnthropic) or `output_token_details.reasoning_tokens`
# (OpenAI). If neither is present, fall back to estimating from the visible
# thinking content.
def extract_reasoning_tokens(
    usage_metadata: UsageMetadata | None,
    *,
    content: str | Sequence[object] | None = None,
    total_reasoning_chars: int = 0,
) -> int:
    if usage_metadata:
        details: Mapping[str, object] = usage_metadata.get("output_token_details") or {}
        from_details = sum(
            _token_count(value, key)
            for key, value in details.items()
            if key == "reasoning" or key.endswith("_reasoning")
        ) or _token_count(details.get("reasoning_tokens"), "reasoning_tokens")
        if from_details:
            return int(from_details)

        reasoning_chars = total_reasoning_chars
        if reasoning_chars == 0 and content is not None:
            reasoning_chars = count_thinking_chars(content)
        if reasoning_chars > 0:
            return max(1, reasoning_chars // 3)
    return 0


# meiva:note
# Count total characters in thinking/reasoning blocks of a message content.
def count_thinking_chars(content: str | Sequence[object]) -> int:
    if isinstance(content, str):
        return 0
    total = 0
    for block in content_blocks(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            total += len(block.get("thinking") or "")
        elif btype == "reasoning":
            for part in block.get("summary") or []:
                total += len(_text_of(part))
    return total
