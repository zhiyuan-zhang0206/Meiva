# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.messages.timestamp_policy import TimestampPolicy
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.projection.model import Segment
from services.derived.insights.run_timeline.schemas import (
    RunTimelineMessage,
    RunTimelineMessagePart,
    RunTimelineMessages,
    RunTimelinePartKind,
)
from services.derived.insights.run_timeline.tokens import span_tokens

LIMIT_MAX = 200

_PART_KIND: dict[str, RunTimelinePartKind] = {
    "agent_reasoning": "think",
    "agent_chat": "text",
    "agent_code": "call",
    "code_output": "out",
    "system_marker": "note",
    "system_prompt": "prompt",
    "inbound_compact_summary": "compact",
    "inbound_compact_request": "compact",
    "inbound_chat": "inbound",
    "attach": "attach",
}


@dataclass(frozen=True)
class MessageSliceSpec:
    start: int
    stop: int
    end: int
    full: bool
    text_max: int
    inputs: TimestampPolicy


def _message(
    view: HistoryView, idx: int, items: list[TimelineItem], *, full: bool, text_max: int
) -> RunTimelineMessage:
    parts: list[RunTimelineMessagePart] = []
    for item in items:
        clipped = not full and len(item.payload) > text_max
        parts.append(
            RunTimelineMessagePart(
                kind=_PART_KIND[item.kind],
                chars=len(item.payload),
                text=item.payload[:text_max] if clipped else item.payload,
                text_truncated=clipped,
            )
        )
    stamp = next((item.created_at for item in items if item.created_at), None)
    ts = datetime.fromisoformat(stamp) if stamp else None
    tokens = span_tokens(view, idx, idx)
    return RunTimelineMessage(
        idx=idx,
        ts=ts,
        source=next((item.source for item in items if item.source), None),
        parts=parts,
        context_tokens=tokens.context_tokens,
        estimated=tokens.estimated,
    )


def slice_messages(
    spans: Sequence[tuple[Segment, HistoryView]],
    spec: MessageSliceSpec,
) -> RunTimelineMessages:
    out: list[RunTimelineMessage] = []
    for segment, view in spans:
        local_start = max(spec.start, segment.message_start) - segment.offset
        local_stop = min(spec.stop + 1, segment.message_end) - segment.offset
        items, _ = build_timeline_items(
            view.history.messages[local_start:local_stop],
            inputs=spec.inputs,
        )
        by_message: dict[int, list[TimelineItem]] = {}
        for item in items:
            by_message.setdefault(local_start + int(item.item_id.split(".")[0]), []).append(item)
        for idx, group in sorted(by_message.items()):
            message = _message(view, idx, group, full=spec.full, text_max=spec.text_max)
            out.append(message.model_copy(update={"idx": idx + segment.offset}))
    return RunTimelineMessages(
        messages=out, next_start=spec.stop + 1 if spec.stop < spec.end else None
    )
