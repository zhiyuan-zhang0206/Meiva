from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from base.lm.harness.events import HarnessCause, TurnOrigin, Usage
from base.lm.harness.session.transcript.entries import (
    AnomalyKind,
    Order,
    ToolStatus,
)

# meiva:note
# What the projection hands the writer: timeline item drafts in the existing `TimelineItem` kind
# vocabulary (the values below are asserted equal to its `kind` literals by a test), what became of the
# inputs we sent, which turns the record shows and how they ended, and what the parser could not use.
# A draft is not an item: the writer assigns the item id and position, resolves reply visibility with
# `resolve_agent_reply` (which lives above this package) and renders a tool call's payload.
# meiva:link base/lm/harness/docs/transcript-projection.md


class DraftKind(StrEnum):
    INBOUND_CHAT = "inbound_chat"
    AGENT_CHAT = "agent_chat"
    AGENT_REASONING = "agent_reasoning"
    AGENT_CODE = "agent_code"
    CODE_OUTPUT = "code_output"
    SUBAGENT_TEXT = "subagent_text"
    SYSTEM_MARKER = "system_marker"


# meiva:note
# `key` is the idempotency key the store keeps unique. `order` and `slot` give the position (`slot`
# orders the several drafts one entry produces). For `agent_code` the payload is empty and
# `tool_name` / `tool_input` carry the call, for the writer to render; `tool_use_id` ties a call to its
# output. `parent_tool_use_id` is set on everything a sub-agent did. `part_ref` names the original
# inbound message an `inbound_chat` draft shows. `complete` is False for text the model did not finish.
@dataclass(frozen=True, slots=True, kw_only=True)
class TimelineDraft:
    key: str
    kind: DraftKind
    payload: str
    order: Order
    slot: int = 0
    record: str
    turn: str | None
    at: str | None = None
    source: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: Mapping[str, object] | None = None
    tool_status: ToolStatus | None = None
    parent_tool_use_id: str | None = None
    part_ref: str | None = None
    images: tuple[str, ...] = ()
    reasoning_ms: int | None = None
    exec_ms: int | None = None
    complete: bool = True


# meiva:note
# One original inbound message inside what was sent as a single input: `ref` is the writer's own name
# for it (an inbound row id), `images` the writer's own references to its images.
@dataclass(frozen=True, slots=True)
class InboundPart:
    ref: str
    text: str
    images: tuple[str, ...] = ()


# meiva:note
# The table of what we sent: one input is one `SentInput`, with the id the CLI will keep for it, the
# whole text as it was sent (for a record that keeps no id) and the original messages it carried.
@dataclass(frozen=True, slots=True)
class SentInput:
    key: str
    wire_id: str
    text: str
    parts: tuple[InboundPart, ...]


class MatchBasis(StrEnum):
    WIRE_ID = "wire_id"
    TEXT = "text"


# meiva:note
# The one delivery rule: an input is delivered when the record shows it. `basis` says how it was
# recognised (the id, or, for a record with none, the text in send order).
@dataclass(frozen=True, slots=True)
class Delivery:
    input_key: str
    basis: MatchBasis
    joined_running_turn: bool
    turn: str | None
    entry_key: str


@dataclass(frozen=True, slots=True)
class TurnObserved:
    turn: str
    origin: TurnOrigin
    cause: HarnessCause | None


@dataclass(frozen=True, slots=True)
class TurnClosed:
    turn: str
    reason: str


@dataclass(frozen=True, slots=True)
class UsageTally:
    usage: Usage
    messages: int
    reliable: bool


@dataclass(frozen=True, slots=True)
class AnomalyReport:
    anomaly: AnomalyKind
    detail: str
    entry_key: str


@dataclass(frozen=True, slots=True)
class Projection:
    drafts: tuple[TimelineDraft, ...] = ()
    deliveries: tuple[Delivery, ...] = ()
    unmatched_inputs: tuple[str, ...] = ()
    turns_observed: tuple[TurnObserved, ...] = ()
    turns_closed: tuple[TurnClosed, ...] = ()
    anomalies: tuple[AnomalyReport, ...] = ()
