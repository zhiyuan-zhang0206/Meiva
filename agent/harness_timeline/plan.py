from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.reply_intent import parse_reply_visibility, resolve_agent_reply
from base.agents.sdk.tally import SdkCall
from base.lm.harness.session.transcript.drafts import (
    AnomalyReport,
    DraftKind,
    MessageUsage,
    Projection,
    TimelineDraft,
    TurnClosed,
    TurnObserved,
)

# meiva:note
# Turn what the projection read into what the writer commits, as a value: the keyed timeline items in the
# order they were read, the inbound rows each delivery settles, the usage to log, the turns that closed, and
# what to report. No I/O: the writer takes the plan into one transaction. Reply visibility
# (`resolve_agent_reply`, which lives above `base/lm`) and the rendering of a tool call are applied here,
# once, so the projection stays free of both. A sub-agent's text and tool output are cut at
# `SUBAGENT_ITEM_CHARS`, as they are on the event path today. A message the CLI cut short (an interrupt) has only a
# placeholder count in the record: it is not priced but is passed on as `unreliable_usage`, which the effects count
# as an event, so the gap in the accounting is a number and not a silence.
# meiva:link agent/harness_timeline/README.md

SUBAGENT_ITEM_CHARS = 8_000
SUBAGENT_CUT = "\n[... sub-agent text cut]"


@dataclass(frozen=True, slots=True)
class ReplyPolicy:
    explicit_visibility: bool
    delivery_mode: str | None


@dataclass(frozen=True, slots=True)
class PlannedItem:
    key: str
    item: TimelineItem
    parent_tool_use_id: str | None


@dataclass(frozen=True, slots=True)
class Settlement:
    input_key: str
    inbound_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WritePlan:
    items: tuple[PlannedItem, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    usage: tuple[MessageUsage, ...] = ()
    unreliable_usage: tuple[MessageUsage, ...] = ()
    closed: tuple[TurnClosed, ...] = ()
    observed: tuple[TurnObserved, ...] = ()
    anomalies: tuple[AnomalyReport, ...] = ()
    unmatched_inputs: tuple[str, ...] = ()
    invalid_reply_turns: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (
            self.items
            or self.settlements
            or self.usage
            or self.closed
            or self.observed
            or self.anomalies
        )


def plan_write(
    projection: Projection,
    *,
    sent_items: Mapping[str, TimelineItem],
    inbound_ids: Mapping[str, tuple[int, ...]],
    non_human_turns: frozenset[str],
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
) -> WritePlan:
    planned: list[PlannedItem] = []
    invalid: list[str] = []
    for draft in projection.drafts:
        non_human = draft.turn in non_human_turns if draft.turn else False
        item = _item_of(
            draft, sent_items, non_human=non_human, policy=policy, render_tool=render_tool
        )
        planned.append(PlannedItem(draft.key, item, draft.parent_tool_use_id))
        if _invalid_reply(draft, policy) and draft.turn is not None:
            invalid.append(draft.turn)
    return WritePlan(
        items=tuple(planned),
        settlements=tuple(
            Settlement(delivery.input_key, inbound_ids.get(delivery.input_key, ()))
            for delivery in projection.deliveries
        ),
        usage=tuple(spend for spend in projection.usage if spend.reliable),
        unreliable_usage=tuple(spend for spend in projection.usage if not spend.reliable),
        closed=projection.turns_closed,
        observed=projection.turns_observed,
        anomalies=projection.anomalies,
        unmatched_inputs=projection.unmatched_inputs,
        invalid_reply_turns=tuple(dict.fromkeys(invalid)),
    )


def _invalid_reply(draft: TimelineDraft, policy: ReplyPolicy) -> bool:
    return (
        draft.kind is DraftKind.AGENT_CHAT
        and draft.complete
        and policy.explicit_visibility
        and bool(draft.payload.strip())
        and parse_reply_visibility(draft.payload) is None
    )


def _item_of(
    draft: TimelineDraft,
    sent_items: Mapping[str, TimelineItem],
    *,
    non_human: bool,
    policy: ReplyPolicy,
    render_tool: Callable[[str, Mapping[str, object]], str],
) -> TimelineItem:
    match draft.kind:
        case DraftKind.INBOUND_CHAT:
            return _inbound(draft, sent_items)
        case DraftKind.AGENT_CHAT:
            payload, user_facing = resolve_agent_reply(
                draft.payload,
                non_human_wake=non_human,
                explicit_visibility=policy.explicit_visibility,
                delivery_mode=policy.delivery_mode,
            )
            return _base(draft, "agent_chat", payload, user_facing=user_facing)
        case DraftKind.AGENT_REASONING:
            return _base(draft, "agent_reasoning", draft.payload, reasoning_ms=draft.reasoning_ms)
        case DraftKind.AGENT_CODE:
            name = draft.tool_name or ""
            return _base(
                draft,
                "agent_code",
                render_tool(name, draft.tool_input or {}),
                source=name if name != "execute_code" else None,
                tool_use_id=draft.tool_use_id,
                sdk_calls=[SdkCall(method=name, count=1)],
            )
        case DraftKind.CODE_OUTPUT:
            return _base(draft, "code_output", _cut(draft, draft.payload), exec_ms=draft.exec_ms)
        case DraftKind.SUBAGENT_TEXT:
            return _base(
                draft,
                "subagent_text",
                _cut(draft, draft.payload),
                source=draft.source,
                user_facing=False,
            )
        case DraftKind.SYSTEM_MARKER:
            return _base(draft, "system_marker", draft.payload, source=draft.source)


def _inbound(draft: TimelineDraft, sent_items: Mapping[str, TimelineItem]) -> TimelineItem:
    prebuilt = sent_items.get(draft.part_ref) if draft.part_ref else None
    if prebuilt is not None:
        return prebuilt
    return _base(draft, "inbound_chat", draft.payload, source=draft.source)


def _cut(draft: TimelineDraft, text: str) -> str:
    if draft.parent_tool_use_id is None or len(text) <= SUBAGENT_ITEM_CHARS:
        return text
    return text[:SUBAGENT_ITEM_CHARS] + SUBAGENT_CUT


def _base(draft: TimelineDraft, kind: str, payload: str, **fields: object) -> TimelineItem:
    return TimelineItem.model_validate(
        {
            **fields,
            "item_id": "pending",
            "kind": kind,
            "payload": payload,
            "created_at": draft.at,
        }
    )
