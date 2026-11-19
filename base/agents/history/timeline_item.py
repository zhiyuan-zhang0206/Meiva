"""The TimelineItem model — one element of the rendered timeline view.

Extracted from `base/agents/history/timeline.py` (file line budget; task #3323): the model
contains the wire fields and their metadata models. The projection logic
that builds the items stays in `base/agents/history/timeline.py`, which re-exports this class so existing
importers keep working.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from base.agents.messages.inbound_images import TimelineUploadFile
from base.agents.messages.quote import Quote
from base.agents.sdk.tally import SdkCall
from base.host.boundary.json_values import is_object_dict, is_object_list


def _missing_reading_locator(value: object) -> bool:
    return value is None


class TimelineItem(BaseModel):
    """One element of a timeline view of LangGraph state.messages, using the
    inbound_messages table as a timestamp anchor and impersonation history
    expanded at its checkpoint marker. External session metadata identifies
    the actual executor without changing the cursor grammar.

    `item_id` is the stable key coordinating frontend timeline with
    streaming SSE; the current segment uses `f"{msg_idx}.{block_idx}"`
    (msg_idx = position in state.messages; for other message types
    block_idx=0). Cold-loaded compact history prefixes that local position
    with `s<rank>.<boundary_checkpoint_id>.`; it never enters the live SSE
    merge path. For an
    AIMessage, text/thinking are content blocks (block_idx = their content
    position) and each tool call gets `block_idx = <number of text/thinking
    content blocks> + <ordinal in msg.tool_calls>` — a provider-agnostic
    rule derived from langchain's normalized `tool_calls` view, so it holds
    whether the provider carries tool calls inside `content` (anthropic
    `tool_use` blocks) or only in the separate `tool_calls` list
    (gemini / openai). During streaming, the frontend uses `*_start`
    event's `item_id` to create items; after commit, the same rule
    recomputes id, and matching ids on both sides means the same logical
    item. On merge, the snapshot version wins, avoiding race bugs from
    ts-heuristic matching.
    """

    item_id: str
    reading_locator: str | None = Field(default=None, exclude_if=_missing_reading_locator)
    quote: Quote | None = None
    activity_pending: bool | None = None
    kind: Literal[
        "inbound_chat",
        "inbound_compact_summary",
        "inbound_compact_request",
        "attach",
        "agent_chat",
        "agent_code",
        "agent_reasoning",
        "code_output",
        "system_prompt",
        "system_marker",
    ]
    source: str | None = None
    payload: str  # rendered content
    created_at: str | None = None  # ISO-8601
    inbound_id: int | None = None
    # Durable source coordinates; item_id remains the frontend position key.
    # Missing/ephemeral source IDs are excluded. Only an explicitly embedded
    # inbound row ID qualifies.
    source_message_id: str | None = None
    source_inbound_id: int | None = None
    source_block_idx: int | None = None
    # The compact run this item belongs to (None elsewhere): a forced/auto
    # compact's summary message carries `meiva_compact_id`, pairing the item
    # with the live compact_started / compact_finished events (ticking
    # "Compacting" block → summary item handoff on the frontend).
    compact_id: str | None = None
    # Reasoning-block summary on `agent_reasoning` items (None elsewhere).
    # Drives the collapsed-reasoning chip ("Thought for 8s / 1.2k tokens") so
    # the user keeps a sense of the thinking without reading it.
    # `reasoning_ms` is per-block: the real wall-clock the llm node measured
    # for that thinking block (agent/graph/_callbacks.py) and persisted on the
    # message — a turn with several thinking blocks carries one value per
    # block. `reasoning_tokens` stays turn-level (usage_metadata reports one
    # total) and sits on the first thinking item only.
    reasoning_ms: int | None = None
    reasoning_tokens: int | None = None
    # Wall-clock the code ran, set only on `code_output` items (None elsewhere).
    # Read from the exec_output message's meiva_exec_ms (agent/graph/exec/node.py).
    # Drives the collapsed-output chip ("ran in 1.3s").
    exec_ms: int | None = None
    # Whether the frontend chip shows this item's wall-clock ts. True everywhere
    # except system_marker notes that read as standing context rather than
    # events (memory recall + the one-time guidance notes) — see
    # `_NO_TIMESTAMP_NOTE_TAGS`. Default True keeps every other kind unchanged.
    show_timestamp: bool = True
    # meiva:note
    # Whether an `agent_chat` item surfaces as a primary user-facing reply in the main dialog
    # stream and reply suggestions. False demotes unprefixed closing/narration text
    # from non-human background wakes into the collapsible `Details` work block while keeping it
    # inspectable and copyable.
    # meiva:link base/agents/history/docs/timeline/timeline.md
    user_facing: bool = True
    # Image urls on a multimodal `inbound_chat` / `attach` item (None
    # elsewhere): inbound_chat = gateway-relative urls from meiva_image_urls
    # (via assetUrl); attach = data URIs from content blocks (rendered raw).
    images: list[str] | None = None
    files: list[TimelineUploadFile] | None = None
    # Per-image caption lines on an `attach` item, aligned 1:1 with `images`:
    # the backend-generated "- [N] name (mime, size) — \"label\"" line that
    # precedes each image block in the message content, so the frontend can
    # interleave every thumbnail with its own label. None on other kinds and on
    # legacy attach messages whose caption was a single text block (no pairing
    # information survives there).
    image_captions: list[str] | None = None
    # SDK calls the block really executed (the runtime tally, read from its
    # exec_output ToolMessage's `sdk_calls`; None until that lands / other kinds).
    sdk_calls: list[SdkCall] | None = None
    # Wall-clock the code-generation took, set only on `agent_code` items (None
    # elsewhere). Read from the AIMessage's meiva_code_ms_by_block (keyed by code
    # block_idx). Drives the detail-block chip "Wrote code for Xs".
    code_elapsed_ms: int | None = None
    turn_id: str | None = None
    turn_duration_ms: int | None = None


class ReadingWindow(BaseModel):
    items: list[TimelineItem]
    reading_source: str | None = None
    anchor_item_id: str | None = None
    older_cursor: str | None = None
    newer_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class TurnTimingSpan:
    turn_id: str
    start_pos: int
    end_pos: int | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TurnTimingContext:
    timings: Sequence[Mapping[str, object]] | Sequence[TurnTimingSpan] = ()
    active_turn_id: str | None = None
    active_turn_started_at: str | None = None
    active_turn_start_idx: int | None = None
    index_shift: int = 0


def _valid_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def extract_checkpoint_turn_timing(
    channel_values: Mapping[str, object],
    *,
    current: bool,
    index_shift: int = 0,
) -> TurnTimingContext:
    raw_timings = channel_values.get("turn_timings")
    spans = (
        parse_turn_timing_spans(
            [e for e in raw_timings if is_object_dict(e)],
            index_shift=index_shift,
        )
        if is_object_list(raw_timings)
        else ()
    )
    if not current or channel_values.get("turn_active") is not True:
        return TurnTimingContext(timings=spans)
    raw_id = channel_values.get("active_turn_id")
    raw_started = channel_values.get("active_turn_started_at")
    raw_start_idx = _valid_int(channel_values.get("active_turn_start_idx"))
    if isinstance(raw_id, str) and raw_id and isinstance(raw_started, str) and raw_started:
        return TurnTimingContext(
            timings=spans,
            active_turn_id=raw_id,
            active_turn_started_at=raw_started,
            active_turn_start_idx=raw_start_idx,
        )
    return TurnTimingContext(timings=spans)


def parse_turn_timing_spans(
    raw_timings: Sequence[Mapping[str, object]],
    *,
    index_shift: int = 0,
) -> tuple[TurnTimingSpan, ...]:
    spans: list[TurnTimingSpan] = []
    for raw in raw_timings:
        turn_id = raw.get("turn_id")
        start_idx = _valid_int(raw.get("start_idx"))
        end_idx = _valid_int(raw.get("end_idx"))
        duration_ms = _valid_int(raw.get("duration_ms"))
        if (
            isinstance(turn_id, str)
            and turn_id
            and start_idx is not None
            and end_idx is not None
            and duration_ms is not None
        ):
            spans.append(
                TurnTimingSpan(
                    turn_id=turn_id,
                    start_pos=start_idx + index_shift,
                    end_pos=end_idx + index_shift,
                    duration_ms=max(duration_ms, 0),
                )
            )
    spans.sort(key=lambda s: s.start_pos)
    return tuple(spans)


def item_position(item: TimelineItem) -> int | None:
    head = item.item_id.split(".", 1)[0].removeprefix("h")
    return int(head) if head.isdigit() else None


def apply_turn_timings(
    items: Sequence[TimelineItem],
    spans: Sequence[TurnTimingSpan],
    *,
    pos_fn: Callable[[TimelineItem], int | None] = item_position,
) -> None:
    if not items or not spans:
        return
    ordered = sorted(spans, key=lambda s: s.start_pos)
    span_idx = 0
    num_spans = len(ordered)
    for item in items:
        if item.kind in (
            "system_prompt",
            "inbound_chat",
            "inbound_compact_summary",
            "compact_boundary",
        ):
            continue
        pos = pos_fn(item)
        if pos is None:
            continue
        while span_idx < num_spans:
            end_pos = ordered[span_idx].end_pos
            if end_pos is not None and pos >= end_pos:
                span_idx += 1
            else:
                break
        if span_idx >= num_spans:
            break
        span = ordered[span_idx]
        if pos >= span.start_pos and (span.end_pos is None or pos < span.end_pos):
            item.turn_id = span.turn_id
            item.turn_duration_ms = span.duration_ms


def open_turn_timing_update(
    *,
    start_idx: int,
    started_at: datetime,
    turn_id: str | None = None,
) -> tuple[dict[str, object], str, str]:
    tid = turn_id or f"meiva-{start_idx}-{int(started_at.timestamp() * 1_000_000)}"
    started_iso = started_at.isoformat()
    return (
        {
            "active_turn_id": tid,
            "active_turn_started_at": started_iso,
            "active_turn_start_idx": start_idx,
        },
        tid,
        started_iso,
    )


def close_turn_timing_update(
    *,
    active_turn_id: str | None,
    active_turn_started_at: str | None,
    active_turn_start_idx: int | None,
    turn_timings: Sequence[Mapping[str, object]],
    end_idx: int,
    ended_at: datetime,
) -> dict[str, object]:
    if active_turn_id is None and active_turn_started_at is None and active_turn_start_idx is None:
        return {}
    timings: list[dict[str, object]] = [dict(entry) for entry in turn_timings]
    start_idx = _valid_int(active_turn_start_idx)
    if (
        isinstance(active_turn_id, str)
        and active_turn_id
        and isinstance(active_turn_started_at, str)
        and active_turn_started_at
        and start_idx is not None
    ):
        started = datetime.fromisoformat(active_turn_started_at)
        duration_ms = max(0, round((ended_at - started).total_seconds() * 1000))
        timings.append(
            {
                "turn_id": active_turn_id,
                "start_idx": start_idx,
                "end_idx": max(end_idx, start_idx),
                "started_at": active_turn_started_at,
                "ended_at": ended_at.isoformat(),
                "duration_ms": duration_ms,
            }
        )
    return {
        "active_turn_id": None,
        "active_turn_started_at": None,
        "active_turn_start_idx": None,
        "turn_timings": timings,
    }
