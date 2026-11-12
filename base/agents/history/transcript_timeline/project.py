from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, item_of
from base.lm.harness.session.transcript.drafts import (
    DraftKind,
    InboundPart,
    SentInput,
    TimelineDraft,
)
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.projection import Projector

# meiva:note
# The timeline of one harness record as a function of its entries: what a record shows is computed from the
# record each time it is asked for and nothing about it is stored. The entries carry their row's byte offset
# as `order.seq` (the parser's `line`), and an item is named `h<offset>.<n>`, `n` counting the items one row
# yields: the file only grows, so the name is the same whichever way the row was reached (a cold read of a
# window, or the live reads that follow the file) and is ordered like the file. The one thing the entries
# cannot say is who a person's message came from: the inputs the host sent are given to `register` (their key and
# the timeline item of each inbox row they carried), and a row of the record whose id is one of theirs shows
# those items, one per message. A person's input no one registered is an input of the host's own (a context
# reset, a recovery note): it is not shown.
# A tool call with no result yet is shown as running (`activity_pending`, revision 1) while the projection says
# it is still open: no later turn has begun and the record has not ended the turn. When its result arrives the
# same item is projected again without the flag. `RecordProjector` keeps what a read-after-read needs (the
# open turn, the unanswered calls) in memory only; a new one over the same rows gives the same items.
# meiva:link base/lm/harness/docs/transcript-projection.md

UNMATCHED = "unmatched"
RUNNING_REVISION = 1


@dataclass(frozen=True, slots=True)
class ProjectedItem:
    key: str
    offset: int
    item: TimelineItem
    turn: str | None
    call_id: str | None
    is_call: bool


# meiva:note
# `turn_start` is where the turn of the newest entry of the feed begins, counting what is not shown (an input of the
# host's own).
@dataclass(frozen=True, slots=True)
class Projected:
    items: tuple[ProjectedItem, ...]
    pending: tuple[ProjectedItem, ...]
    turns_closed: int
    opened_turns: frozenset[str]
    turn_start: int | None


class RecordProjector:
    def __init__(
        self,
        *,
        policy: ReplyPolicy,
        render_tool: Callable[[str, Mapping[str, object]], str],
        wire_id: Callable[[str], str],
    ) -> None:
        self._policy = policy
        self._render = render_tool
        self._wire_id = wire_id
        self._projector = Projector()
        self._sent_items: dict[str, TimelineItem] = {}
        self._registered: set[str] = set()
        self._non_human: set[str] = set()
        self._open: dict[str, ProjectedItem] = {}

    def knows(self, key: str) -> bool:
        return key in self._registered

    def register(self, key: str, items: Sequence[TimelineItem]) -> None:
        refs = tuple(f"{key}:{index}" for index in range(len(items)))
        self._sent_items.update(zip(refs, items, strict=True))
        self._projector.register(
            SentInput(
                key=key,
                wire_id=self._wire_id(key),
                text="",
                parts=tuple(
                    InboundPart(ref=ref, text=item.payload)
                    for ref, item in zip(refs, items, strict=True)
                ),
            )
        )
        self._registered.add(key)

    def feed(self, entries: Iterable[AnyEntry], *, tail: bool, release_held: bool) -> Projected:
        projection = self._projector.feed(entries, release_held=release_held)
        for observed in projection.turns_observed:
            if observed.origin.value == "input":
                self._non_human.discard(observed.turn)
            else:
                self._non_human.add(observed.turn)
        unanswered: frozenset[str] = (
            frozenset(self._projector.unanswered_calls()) if tail else frozenset[str]()
        )
        every = projection.drafts
        drafts = sorted(
            (draft for draft in projection.drafts if draft.source != UNMATCHED),
            key=lambda draft: (draft.order.seq, draft.order.sub, draft.slot),
        )
        made: list[ProjectedItem] = []
        counts: dict[int, int] = {}
        for draft in drafts:
            index = counts.get(draft.order.seq, 0)
            counts[draft.order.seq] = index + 1
            made.append(self._project(draft, index, unanswered))
        out = [*made, *self._settle_open(made, unanswered)]
        return Projected(
            items=tuple(out),
            pending=tuple(self._open.values()),
            turns_closed=len(projection.turns_closed),
            opened_turns=frozenset(observed.turn for observed in projection.turns_observed),
            turn_start=_turn_start(every),
        )

    # meiva:note
    # The calls shown as running that the projection now says are done: the same items, without the flag. A call
    # that is still open is remembered so the read that finds its result can settle it.
    def _settle_open(
        self, made: Sequence[ProjectedItem], unanswered: frozenset[str]
    ) -> list[ProjectedItem]:
        settled: list[ProjectedItem] = []
        for call_id, held in list(self._open.items()):
            if call_id not in unanswered:
                del self._open[call_id]
                if all(item.key != held.key for item in made):
                    settled.append(_settled(held))
        for item in made:
            if item.is_call and item.call_id is not None and item.call_id in unanswered:
                self._open[item.call_id] = item
        return settled

    def _project(
        self, draft: TimelineDraft, index: int, unanswered: frozenset[str]
    ) -> ProjectedItem:
        item = item_of(
            draft,
            self._sent_items,
            non_human=draft.turn in self._non_human if draft.turn else False,
            policy=self._policy,
            render_tool=self._render,
        )
        is_call = draft.kind is DraftKind.AGENT_CODE
        update: dict[str, object] = {"item_id": f"h{draft.order.seq}.{index}"}
        if item.created_at:
            update["created_at"] = _normalized(item.created_at)
        if is_call and draft.tool_use_id in unanswered:
            update.update(activity_pending=True, activity_revision=RUNNING_REVISION)
        return ProjectedItem(
            key=draft.key,
            offset=draft.order.seq,
            item=item.model_copy(update=update),
            turn=draft.turn,
            call_id=draft.tool_use_id,
            is_call=is_call,
        )


def _settled(held: ProjectedItem) -> ProjectedItem:
    return ProjectedItem(
        key=held.key,
        offset=held.offset,
        item=held.item.model_copy(update={"activity_pending": None, "activity_revision": None}),
        turn=held.turn,
        call_id=held.call_id,
        is_call=True,
    )


def _normalized(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).isoformat()
    except ValueError:
        return stamp


def _turn_start(drafts: Sequence[TimelineDraft]) -> int | None:
    if not drafts:
        return None
    last = max(drafts, key=lambda draft: draft.order)
    return min(draft.order.seq for draft in drafts if draft.turn == last.turn)
