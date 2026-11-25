from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace

from base.lm.harness.events import HarnessCause, TurnOrigin, Usage
from base.lm.harness.session.transcript.drafts import (
    AnomalyReport,
    Delivery,
    DraftKind,
    MatchBasis,
    MessageUsage,
    Projection,
    SentInput,
    TimelineDraft,
    TurnClosed,
    TurnObserved,
    UsageTally,
)
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    Entry,
    Failure,
    HumanInput,
    HumanOrigin,
    Interrupt,
    Notice,
    Order,
    Reasoning,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolStatus,
    TurnEnd,
    TurnEndReason,
)


# meiva:note
# The two containers the projector keeps its memory of a long conversation in: insertion-ordered, with a fixed upper
# bound, so that what it remembers never grows with the length of the conversation. When a container is full the
# oldest entry is forgotten first; re-adding or re-setting a present item keeps its place in the order.
# meiva:link base/lm/harness/docs/transcript-projection.md
class RecentSet[T]:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[T, None] = {}

    def add(self, item: T) -> None:
        if item in self._items:
            return
        self._items[item] = None
        if len(self._items) > self._limit:
            del self._items[next(iter(self._items))]

    def discard(self, item: T) -> None:
        _ = self._items.pop(item, None)

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)


class RecentMap[K, V]:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[K, V] = {}

    def __setitem__(self, key: K, value: V) -> None:
        self._items[key] = value
        if len(self._items) > self._limit:
            del self._items[next(iter(self._items))]

    def __getitem__(self, key: K) -> V:
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def get(self, key: K) -> V | None:
        return self._items.get(key)

    def pop(self, key: K) -> V | None:
        return self._items.pop(key, None)

    def values(self) -> Iterator[V]:
        return iter(self._items.values())

    def items(self) -> Iterator[tuple[K, V]]:
        return iter(self._items.items())


# meiva:note
# What the projector remembers, and for how long. Everything that grows with the conversation is bounded, so the
# projector's memory does not depend on its length: the keys of projected entries (a safety net against reading the
# same entry twice; the reader never repeats a row), the open and recent calls, the turns seen and closed,
# the per-message usage and the reported anomalies are each the latest N, the oldest forgotten first.
# What a forgotten item meant is stated where it matters: a result whose call was forgotten waits like any other
# result whose call has not arrived. Nothing is held for long. A result whose call has not been read is held for
# at most `HOLD_ENTRIES` further entries (or until `feed(release_held=True)`); then it is put on the timeline
# with one `ORPHAN_RELEASED` anomaly. A call still unanswered when its turn
# closes, or when the next turn on the record begins, is abandoned: it no longer keeps the record from being
# quiet, and a result that arrives later is still paired with it.
# meiva:link base/lm/harness/docs/transcript-projection.md
SEEN_LIMIT = 20_000
CALLS_LIMIT = 4_096
TURNS_LIMIT = 1_024
USAGE_LIMIT = 8_192
REPORTED_LIMIT = 1_024
HOLD_ENTRIES = 2_000

INTERRUPT_MARKER = "status:Turn interrupted"
COMPACTION_MARKER = "status:Context compacted"
_IMPLICIT_TURN = "implicit"


@dataclass(frozen=True, slots=True)
class _Call:
    order: Order
    turn: str
    answered: bool = False
    abandoned: bool = False


@dataclass(frozen=True, slots=True)
class _Orphaned:
    born: int
    results: list[ToolResult]


# meiva:note
# The one projection: entries in, timeline drafts and delivery conclusions out. It is the only place
# that decides how a call is paired with its output, how an input we sent is recognised in the record
# and what a turn's spend is. Non-main records contribute only anomalies and separate usage tallies.
# It does no I/O and reads no clock; its only state is what the entries so far established, so the same
# entries in the same order give the same projection, and an entry whose key was already projected gives
# nothing. A command result that arrives before its call waits for the call, and the drafts carry their
# `order` so the caller places them.
# meiva:link base/lm/harness/docs/transcript-projection.md
class Projector:
    def __init__(self, *, known_keys: Iterable[str] = ()) -> None:
        self._seen: RecentSet[str] = RecentSet(SEEN_LIMIT)
        for key in known_keys:
            self._seen.add(key)
        self._fed = 0
        self._sent: dict[str, SentInput] = {}
        self._unsettled: list[SentInput] = []
        self._turns_seen: RecentSet[str] = RecentSet(TURNS_LIMIT)
        self._current: str | None = None
        self._calls: RecentMap[str, _Call] = RecentMap(CALLS_LIMIT)
        self._inferred: RecentSet[str] = RecentSet(TURNS_LIMIT)
        self._closed: RecentSet[str] = RecentSet(TURNS_LIMIT)
        self._orphans: dict[str, _Orphaned] = {}
        self._usage: RecentMap[tuple[str, str], TokenUsage] = RecentMap(USAGE_LIMIT)
        self._unreliable: RecentSet[tuple[str, str]] = RecentSet(USAGE_LIMIT)
        self._reported: RecentSet[tuple[str, str]] = RecentSet(REPORTED_LIMIT)
        self._out = _Out()

    def register(self, sent: SentInput) -> None:
        self._sent[sent.wire_id] = sent
        self._unsettled.append(sent)

    def unregister(self, key: str) -> None:
        sent = next((item for item in self._unsettled if item.key == key), None)
        if sent is not None:
            self._unsettled.remove(sent)
            _ = self._sent.pop(sent.wire_id, None)

    def feed(self, entries: Iterable[AnyEntry], *, release_held: bool = False) -> Projection:
        self._out = _Out()
        for entry in entries:
            if entry.key in self._seen:
                continue
            self._seen.add(entry.key)
            self._fed += 1
            self._dispatch(entry)
        self._release(everything=release_held)
        return self._out.freeze()

    def _release(self, *, everything: bool) -> None:
        for call_id, orphaned in list(self._orphans.items()):
            if everything or self._fed - orphaned.born >= HOLD_ENTRIES:
                del self._orphans[call_id]
                self._release_results(call_id, orphaned.results)

    def _release_results(self, call_id: str, results: list[ToolResult]) -> None:
        for result in results:
            self._out.anomalies.append(
                AnomalyReport(AnomalyKind.ORPHAN_RELEASED, call_id, result.key)
            )
            self._draft(
                result,
                DraftKind.CODE_OUTPUT,
                result.content or ("Tool failed" if _failed(result) else ""),
                _Extra(
                    tool_use_id=result.call_id,
                    tool_status=result.status,
                    exec_ms=result.duration_ms,
                ),
            )

    def usage_by_turn(self) -> dict[str, UsageTally]:
        return self._tally(lambda record: record == MAIN_RECORD, by_turn=True)

    def usage_by_record(self) -> dict[str, UsageTally]:
        return self._tally(lambda _: True, by_turn=False)

    # meiva:note
    # The size of each thing the projector remembers, by name, for a test or a gauge to check that it stays bounded.
    def memory_sizes(self) -> dict[str, int]:
        return {
            "seen": len(self._seen),
            "calls": len(self._calls),
            "turns": len(self._turns_seen),
            "closed": len(self._closed),
            "usage": len(self._usage),
            "orphans": len(self._orphans),
            "reported": len(self._reported),
        }

    def unresolved_results(self) -> tuple[str, ...]:
        return tuple(self._orphans)

    def unanswered_calls(self) -> tuple[str, ...]:
        return tuple(
            call_id
            for call_id, call in self._calls.items()
            if not call.answered and not call.abandoned
        )

    def undelivered(self) -> tuple[SentInput, ...]:
        return tuple(self._unsettled)

    def _tally(self, keep: Callable[[str], bool], *, by_turn: bool) -> dict[str, UsageTally]:
        tallies: dict[str, UsageTally] = {}
        for (record, ref), entry in self._usage.items():
            if not keep(record):
                continue
            name = (entry.turn or _IMPLICIT_TURN) if by_turn else record
            previous = tallies.get(name, UsageTally(usage=Usage(), messages=0, reliable=True))
            tallies[name] = UsageTally(
                usage=previous.usage + entry.usage,
                messages=previous.messages + 1,
                reliable=previous.reliable and (record, ref) not in self._unreliable,
            )
        return tallies

    def _dispatch(self, entry: AnyEntry) -> None:
        if entry.record != MAIN_RECORD and not isinstance(entry, (Anomaly, TokenUsage)):
            return
        match entry:
            case Anomaly():
                self._on_anomaly(entry)
            case TokenUsage():
                self._on_usage(entry)
            case ToolCall():
                self._on_call(entry)
            case ToolResult():
                self._on_result(entry)
            case AgentText() | Reasoning():
                self._on_text(entry)
            case HumanInput():
                self._on_human(entry)
            case Notice():
                self._on_notice(entry)
            case Interrupt() | Compaction() | Failure() | TurnEnd():
                self._on_marker(entry)

    def _turn(self, entry: Entry, *, opens: bool = False) -> str:
        name = entry.turn or (f"{entry.record}:{entry.key}" if opens else self._current)
        if name is None:
            return _IMPLICIT_TURN
        if name not in self._turns_seen:
            if self._current is not None:
                self._abandon(self._current)
            self._turns_seen.add(name)
            self._current = name
            if not opens:
                self._inferred.add(name)
            self._observe(name, entry)
        elif opens and name in self._inferred:
            self._inferred.discard(name)
            self._observe(name, entry)
        return name

    def _observe(self, name: str, entry: Entry) -> None:
        if isinstance(entry, HumanInput):
            self._out.observed.append(TurnObserved(name, TurnOrigin.INPUT, None))
        else:
            cause = entry.cause if isinstance(entry, Notice) else HarnessCause.UNKNOWN
            self._out.observed.append(TurnObserved(name, TurnOrigin.HARNESS, cause))

    def _emit(self, draft: TimelineDraft) -> None:
        self._out.drafts.append(draft)

    def _draft(
        self, entry: Entry, kind: DraftKind, payload: str, extra: _Extra | None = None
    ) -> None:
        more = extra or _Extra()
        self._emit(
            TimelineDraft(
                key=entry.key,
                kind=kind,
                payload=payload,
                order=entry.order,
                record=entry.record,
                turn=self._turn(entry),
                at=entry.at,
                source=more.source,
                tool_use_id=more.tool_use_id,
                tool_name=more.tool_call[0] if more.tool_call else None,
                tool_input=more.tool_call[1] if more.tool_call else None,
                tool_status=more.tool_status,
                reasoning_ms=more.reasoning_ms,
                exec_ms=more.exec_ms,
                complete=more.complete,
                block=more.block,
            )
        )

    def _on_anomaly(self, entry: Anomaly) -> None:
        mark = (entry.anomaly.value, entry.detail)
        if mark in self._reported:
            return
        self._reported.add(mark)
        self._out.anomalies.append(AnomalyReport(entry.anomaly, entry.detail, entry.key))

    def _on_usage(self, entry: TokenUsage) -> None:
        ident = (entry.record, entry.message_ref)
        if ident in self._usage:
            if not entry.reliable:
                self._unreliable.add(ident)
            return
        turn = (
            entry.turn or _IMPLICIT_TURN
            if entry.record != MAIN_RECORD
            else (entry.turn or self._turn(entry))
        )
        stored = entry if entry.turn == turn else replace(entry, turn=turn)
        self._usage[ident] = stored
        if not entry.reliable:
            self._unreliable.add(ident)
        self._out.usage.append(
            MessageUsage(
                record=entry.record,
                message_ref=entry.message_ref,
                turn=stored.turn,
                usage=entry.usage,
                model=entry.model,
                reliable=entry.reliable,
            )
        )

    def _on_text(self, entry: AgentText | Reasoning) -> None:
        if not entry.text.strip():
            return
        if isinstance(entry, Reasoning):
            self._draft(
                entry,
                DraftKind.AGENT_REASONING,
                entry.text,
                _Extra(reasoning_ms=entry.duration_ms, block=entry.block),
            )
        else:
            self._draft(
                entry,
                DraftKind.AGENT_CHAT,
                entry.text,
                _Extra(complete=entry.complete, block=entry.block),
            )

    def _on_call(self, entry: ToolCall) -> None:
        self._draft(
            entry,
            DraftKind.AGENT_CODE,
            "",
            _Extra(tool_use_id=entry.call_id, tool_call=(entry.name, entry.tool_input)),
        )
        self._calls[entry.call_id] = _Call(entry.order, self._turn(entry))
        orphaned = self._orphans.pop(entry.call_id, None)
        for result in () if orphaned is None else orphaned.results:
            self._on_result(result)

    def _on_result(self, entry: ToolResult) -> None:
        started = self._calls.get(entry.call_id)
        if started is None:
            orphaned = self._orphans.get(entry.call_id)
            if orphaned is None:
                orphaned = self._orphans[entry.call_id] = _Orphaned(self._fed, [])
            orphaned.results.append(entry)
            return
        self._calls[entry.call_id] = replace(started, answered=True)
        self._draft(
            entry,
            DraftKind.CODE_OUTPUT,
            entry.content or ("Tool failed" if _failed(entry) else ""),
            _Extra(tool_use_id=entry.call_id, tool_status=entry.status, exec_ms=entry.duration_ms),
        )

    # meiva:note
    # What a row that the record files as an input is, decided here and nowhere else, in this order: (1) its wire id is
    # one of the inputs we registered: it is ours, and it settles, whatever origin the row says (a newer CLI may tag
    # our own inputs with an origin we have never seen, and an input that is not settled is sent again); (2) its origin
    # is one the vocabulary knows (a finished task, a timer): a notice; (3) it names no origin, or `human`: an input
    # we did not send (unmatched), shown as such; (4) any other origin: an anomaly, reported and not shown, because
    # nothing can say what it is.
    def _on_human(self, entry: HumanInput) -> None:
        sent, basis = self._recognise(entry)
        if sent is None and entry.origin is not None:
            self._on_origin(entry, entry.origin)
            return
        turn = self._turn(entry, opens=not entry.joined_running_turn)
        if sent is None:
            self._out.unmatched.append(entry.key)
            self._draft(entry, DraftKind.INBOUND_CHAT, entry.text, _Extra(source="unmatched"))
            return
        if basis is None:
            return
        self._out.deliveries.append(
            Delivery(sent.key, basis, entry.joined_running_turn, turn, entry.key)
        )
        for slot, part in enumerate(sent.parts):
            self._emit(
                TimelineDraft(
                    key=f"{entry.key}#{part.ref}",
                    kind=DraftKind.INBOUND_CHAT,
                    payload=part.text,
                    order=entry.order,
                    slot=slot,
                    record=entry.record,
                    turn=turn,
                    at=entry.at,
                    part_ref=part.ref,
                    images=part.images,
                )
            )

    def _on_origin(self, entry: HumanInput, origin: HumanOrigin) -> None:
        if origin.cause is HarnessCause.UNKNOWN:
            self._on_anomaly(
                Anomaly(
                    key=f"{entry.key}:anomaly:origin",
                    order=entry.order,
                    record=entry.record,
                    anomaly=AnomalyKind.UNKNOWN_TYPE,
                    detail=f"origin/{origin.kind}",
                )
            )
            return
        self._on_notice(
            Notice(
                key=entry.key,
                order=entry.order,
                record=entry.record,
                turn=entry.turn,
                at=entry.at,
                text=origin.text,
                cause=origin.cause,
                call_id=origin.call_id,
                opens_turn=not entry.joined_running_turn,
            )
        )

    def _recognise(self, entry: HumanInput) -> tuple[SentInput | None, MatchBasis | None]:
        if entry.wire_id is not None and entry.wire_id in self._sent:
            sent = self._sent[entry.wire_id]
            return sent, self._settle(sent, MatchBasis.WIRE_ID)
        if entry.wire_id is not None or entry.origin is not None:
            return None, None
        text = entry.text.strip()
        for sent in self._unsettled:
            if sent.text.strip() == text:
                return sent, self._settle(sent, MatchBasis.TEXT)
        return None, None

    def _settle(self, sent: SentInput, basis: MatchBasis) -> MatchBasis | None:
        if sent not in self._unsettled:
            return None
        self._unsettled.remove(sent)
        return basis

    def _on_notice(self, entry: Notice) -> None:
        if entry.opens_turn:
            _ = self._turn(entry, opens=True)
        self._draft(
            entry,
            DraftKind.SYSTEM_MARKER,
            entry.text,
            _Extra(source=entry.cause.value, tool_use_id=entry.call_id),
        )

    def _on_marker(self, entry: Interrupt | Compaction | Failure | TurnEnd) -> None:
        turn = self._turn(entry)
        match entry:
            case Interrupt():
                self._draft(entry, DraftKind.SYSTEM_MARKER, INTERRUPT_MARKER)
                self._close(turn, "interrupted")
            case Compaction():
                self._draft(entry, DraftKind.SYSTEM_MARKER, COMPACTION_MARKER)
            case Failure():
                self._draft(entry, DraftKind.SYSTEM_MARKER, f"error:{entry.message}")
                self._close(turn, TurnEndReason.FAILED.value)
            case TurnEnd():
                self._close(turn, entry.reason.value)

    def _close(self, turn: str, reason: str) -> None:
        if turn in self._closed:
            return
        self._closed.add(turn)
        self._abandon(turn)
        self._out.closed.append(TurnClosed(turn, reason))

    def _abandon(self, turn: str) -> None:
        for call_id, call in list(self._calls.items()):
            if call.turn == turn and not call.answered:
                self._calls[call_id] = replace(call, abandoned=True)


def _failed(result: ToolResult) -> bool:
    return result.status in (ToolStatus.ERROR, ToolStatus.DENIED, ToolStatus.ABORTED)


class _Out:
    def __init__(self) -> None:
        self.drafts: list[TimelineDraft] = []
        self.deliveries: list[Delivery] = []
        self.unmatched: list[str] = []
        self.observed: list[TurnObserved] = []
        self.closed: list[TurnClosed] = []
        self.anomalies: list[AnomalyReport] = []
        self.usage: list[MessageUsage] = []

    def freeze(self) -> Projection:
        return Projection(
            drafts=tuple(self.drafts),
            deliveries=tuple(self.deliveries),
            unmatched_inputs=tuple(self.unmatched),
            turns_observed=tuple(self.observed),
            turns_closed=tuple(self.closed),
            anomalies=tuple(self.anomalies),
            usage=tuple(self.usage),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Extra:
    source: str | None = None
    tool_use_id: str | None = None
    tool_call: tuple[str, Mapping[str, object]] | None = None
    tool_status: ToolStatus | None = None
    reasoning_ms: int | None = None
    exec_ms: int | None = None
    complete: bool = True
    block: str | None = None
