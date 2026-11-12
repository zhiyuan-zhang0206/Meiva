from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, item_of
from base.agents.messages.reply_intent import parse_reply_visibility
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


@dataclass(frozen=True, slots=True)
class PlannedItem:
    key: str
    item: TimelineItem
    parent_tool_use_id: str | None


@dataclass(frozen=True, slots=True)
class Settlement:
    input_key: str
    inbound_ids: tuple[int, ...]
    turn: str | None = None


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
        item = item_of(
            draft, sent_items, non_human=non_human, policy=policy, render_tool=render_tool
        )
        planned.append(PlannedItem(draft.key, item, draft.parent_tool_use_id))
        if _invalid_reply(draft, policy) and draft.turn is not None:
            invalid.append(draft.turn)
    return WritePlan(
        items=tuple(planned),
        settlements=tuple(
            Settlement(delivery.input_key, inbound_ids.get(delivery.input_key, ()), delivery.turn)
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
