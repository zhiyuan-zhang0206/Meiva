"""The closing request of a sealed segment: the compaction LLM call that read it whole.

A compaction summarised by an LLM (`compact_kind` `auto` / `compact_request`) sends the
segment's whole conversation plus one instruction message; the provider reports that
request's `input_tokens`. It is the only request that sees the segment's tail (the messages
after its last AIMessage), so it anchors the tail's token counts. The compaction stamps it into
the boundary checkpoint's metadata (`mark_compact_boundary`) and the history read hands it back
per segment (`FullHistory.segment_closings`). An agent-written summary makes no such call and
leaves no anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from langchain_core.messages import BaseMessage, SystemMessage

# The boundary checkpoint's metadata key holding the anchor.
METADATA_KEY = "compact_anchor"


@dataclass(frozen=True)
class ClosingRequest:
    """`input_tokens` is the closing request's provider-reported input; `extra_tokens` what it
    added beyond the segment's messages (the compaction instruction, estimated), subtracted
    before the tail is anchored -- non-zero makes the tail estimated. `model` is the model that
    answered (a different model than the segment's last request re-anchors)."""

    input_tokens: int
    extra_tokens: int = 0
    model: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "extra_tokens": self.extra_tokens,
            "model": self.model,
        }

    @classmethod
    def from_metadata(cls, raw: object) -> ClosingRequest | None:
        """The anchor stored in a boundary's metadata; None when the compaction left none
        (an agent-written summary makes no closing call)."""
        if not isinstance(raw, dict):
            return None
        data: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        tokens, extra, model = (
            data.get("input_tokens"),
            data.get("extra_tokens", 0),
            data.get("model"),
        )
        if not isinstance(tokens, int) or tokens <= 0 or not isinstance(extra, int):
            return None
        return cls(tokens, extra, model if isinstance(model, str) else None)


# meiva:note
# Stitched message history paired with per-segment leading SystemMessage heads, body start indices, and closing anchors.
class FullHistory(NamedTuple):
    messages: list[BaseMessage]
    segment_heads: tuple[SystemMessage | None, ...]
    segment_starts: tuple[int, ...]
    segment_closings: tuple[ClosingRequest | None, ...] = ()


# meiva:note
# Wraps a single uncompacted message list into a 1-segment FullHistory separating any leading SystemMessage head.
def single_segment_history(messages: list[BaseMessage]) -> FullHistory:
    if not messages:
        return FullHistory([], (), ())
    head = messages[0] if isinstance(messages[0], SystemMessage) else None
    return FullHistory(messages, (head,), (1 if head is not None else 0,))
