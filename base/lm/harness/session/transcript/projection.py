from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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
    AnyEntry,
    Compaction,
    Entry,
    Failure,
    HumanInput,
    Interrupt,
    Notice,
    Order,
    Reasoning,
    SubagentLink,
    TokenUsage,
    ToolCall,
    ToolResult,
    ToolStatus,
    TurnEnd,
    TurnEndReason,
)

INTERRUPT_MARKER = "status:Turn interrupted"
COMPACTION_MARKER = "status:Context compacted"
_IMPLICIT_TURN = "implicit"


# meiva:note
# The one projection: entries in, timeline drafts and delivery conclusions out. It is the only place
# that decides how a call is paired with its output, how a sub-agent's activity nests under the call
# that started it, how an input we sent is recognised in the record and what a turn's spend is. It does
# no I/O and reads no clock; its only state is what the entries so far established, so the same entries
# in the same order give the same projection, and an entry whose key was already projected gives
# nothing. Entries may arrive out of conversation order (a sub-agent record before the link to it, a
# command result before its call): pairing and nesting wait for the missing half, and the drafts carry
# their `order` so the writer places them.
# meiva:link base/lm/harness/docs/transcript-projection.md
class Projector:
    def __init__(self, *, known_keys: Iterable[str] = ()) -> None:
        self._seen: set[str] = set(known_keys)
        self._sent: dict[str, SentInput] = {}
        self._unsettled: list[SentInput] = []
        self._turns_seen: set[str] = set()
        self._current: str | None = None
        self._calls: dict[str, tuple[Order, str]] = {}
        self._answered: set[str] = set()
        self._inferred: set[str] = set()
        self._closed: set[str] = set()
        self._orphans: dict[str, list[ToolResult]] = {}
        self._links: dict[str, str] = {}
        self._held: dict[str, list[TimelineDraft]] = {}
        self._usage: dict[tuple[str, str], TokenUsage] = {}
        self._unreliable: set[tuple[str, str]] = set()
        self._held_usage: dict[str, list[MessageUsage]] = {}
        self._reported: set[tuple[str, str]] = set()
        self._out = _Out()

    def register(self, sent: SentInput) -> None:
        self._sent[sent.wire_id] = sent
        self._unsettled.append(sent)

    def feed(self, entries: Iterable[AnyEntry]) -> Projection:
        self._out = _Out()
        for entry in entries:
            if entry.key in self._seen:
                continue
            self._seen.add(entry.key)
            self._dispatch(entry)
        return self._out.freeze()

    def usage_by_turn(self) -> dict[str, UsageTally]:
        return self._tally(lambda record: record == MAIN_RECORD, by_turn=True)

    def usage_by_record(self) -> dict[str, UsageTally]:
        return self._tally(lambda _: True, by_turn=False)

    def unresolved_results(self) -> tuple[str, ...]:
        return tuple(self._orphans)

    def unanswered_calls(self) -> tuple[str, ...]:
        return tuple(call for call in self._calls if call not in self._answered)

    def undelivered(self) -> tuple[SentInput, ...]:
        return tuple(self._unsettled)

    def unlinked_records(self) -> tuple[str, ...]:
        return tuple(self._held)

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
        match entry:
            case Anomaly():
                self._on_anomaly(entry)
            case SubagentLink():
                self._on_link(entry)
            case TokenUsage():
                self._on_usage(entry)
            case ToolCall():
                self._on_call(entry)
            case ToolResult():
                self._on_result(entry)
            case AgentText() | Reasoning():
                self._on_text(entry)
            case HumanInput() if entry.record == MAIN_RECORD:
                self._on_human(entry)
            case Notice():
                self._on_notice(entry)
            case Interrupt() | Compaction() | Failure() | TurnEnd():
                self._on_marker(entry)
            case _:
                pass

    def _turn(self, entry: Entry, *, opens: bool = False) -> str:
        if entry.record != MAIN_RECORD:
            return self._sub_turn(entry)
        name = entry.turn or (f"{entry.record}:{entry.key}" if opens else self._current)
        if name is None:
            return _IMPLICIT_TURN
        if name not in self._turns_seen:
            self._turns_seen.add(name)
            self._current = name
            if not opens:
                self._inferred.add(name)
            self._observe(name, entry)
        elif opens and name in self._inferred:
            self._inferred.discard(name)
            self._observe(name, entry)
        return name

    def _sub_turn(self, entry: Entry) -> str:
        call = self._links.get(entry.record)
        if call is not None and call in self._calls:
            return self._calls[call][1]
        return entry.turn or _IMPLICIT_TURN

    def _observe(self, name: str, entry: Entry) -> None:
        if isinstance(entry, HumanInput):
            self._out.observed.append(TurnObserved(name, TurnOrigin.INPUT, None))
        else:
            cause = entry.cause if isinstance(entry, Notice) else HarnessCause.UNKNOWN
            self._out.observed.append(TurnObserved(name, TurnOrigin.HARNESS, cause))

    def _emit(self, draft: TimelineDraft) -> None:
        if draft.record != MAIN_RECORD and draft.record not in self._links:
            self._held.setdefault(draft.record, []).append(draft)
            return
        parent = self._links.get(draft.record)
        self._out.drafts.append(replace(draft, parent_tool_use_id=parent) if parent else draft)

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
            )
        )

    def _on_anomaly(self, entry: Anomaly) -> None:
        mark = (entry.anomaly.value, entry.detail)
        if mark in self._reported:
            return
        self._reported.add(mark)
        self._out.anomalies.append(AnomalyReport(entry.anomaly, entry.detail, entry.key))

    def _on_link(self, entry: SubagentLink) -> None:
        self._links[entry.child_record] = entry.call_id
        call = self._calls.get(entry.call_id)
        for draft in self._held.pop(entry.child_record, []):
            self._emit(replace(draft, turn=call[1]) if call else draft)
        for spend in self._held_usage.pop(entry.child_record, []):
            self._out.usage.append(self._attributed(spend))

    def _on_usage(self, entry: TokenUsage) -> None:
        ident = (entry.record, entry.message_ref)
        if ident in self._usage:
            if not entry.reliable:
                self._unreliable.add(ident)
            return
        stored = entry if entry.turn else replace(entry, turn=self._turn(entry))
        self._usage[ident] = stored
        if not entry.reliable:
            self._unreliable.add(ident)
        self._report_usage(
            MessageUsage(
                record=entry.record,
                message_ref=entry.message_ref,
                turn=stored.turn,
                parent_call_id=None,
                usage=entry.usage,
                model=entry.model,
                reliable=entry.reliable,
            )
        )

    def _report_usage(self, spend: MessageUsage) -> None:
        if spend.record == MAIN_RECORD:
            self._out.usage.append(spend)
        elif spend.record in self._links:
            self._out.usage.append(self._attributed(spend))
        else:
            self._held_usage.setdefault(spend.record, []).append(spend)

    def _attributed(self, spend: MessageUsage) -> MessageUsage:
        call = self._links[spend.record]
        known = self._calls.get(call)
        return replace(spend, parent_call_id=call, turn=known[1] if known else spend.turn)

    def _on_text(self, entry: AgentText | Reasoning) -> None:
        if not entry.text.strip():
            return
        sub = entry.record != MAIN_RECORD
        if isinstance(entry, Reasoning):
            if not sub:
                self._draft(
                    entry,
                    DraftKind.AGENT_REASONING,
                    entry.text,
                    _Extra(reasoning_ms=entry.duration_ms),
                )
        elif sub:
            self._draft(entry, DraftKind.SUBAGENT_TEXT, entry.text)
        else:
            self._draft(entry, DraftKind.AGENT_CHAT, entry.text, _Extra(complete=entry.complete))

    def _on_call(self, entry: ToolCall) -> None:
        self._draft(
            entry,
            DraftKind.AGENT_CODE,
            "",
            _Extra(tool_use_id=entry.call_id, tool_call=(entry.name, entry.tool_input)),
        )
        self._calls[entry.call_id] = (entry.order, self._turn(entry))
        for result in self._orphans.pop(entry.call_id, []):
            self._on_result(result)

    def _on_result(self, entry: ToolResult) -> None:
        started = self._calls.get(entry.call_id)
        self._answered.add(entry.call_id)
        if started is None:
            self._orphans.setdefault(entry.call_id, []).append(entry)
            return
        failed = entry.status in (ToolStatus.ERROR, ToolStatus.DENIED, ToolStatus.ABORTED)
        self._draft(
            entry,
            DraftKind.CODE_OUTPUT,
            entry.content or ("Tool failed" if failed else ""),
            _Extra(tool_use_id=entry.call_id, tool_status=entry.status, exec_ms=entry.duration_ms),
        )

    def _on_human(self, entry: HumanInput) -> None:
        turn = self._turn(entry, opens=not entry.joined_running_turn)
        sent, basis = self._recognise(entry)
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

    def _recognise(self, entry: HumanInput) -> tuple[SentInput | None, MatchBasis | None]:
        if entry.wire_id is not None and entry.wire_id in self._sent:
            sent = self._sent[entry.wire_id]
            return sent, self._settle(sent, MatchBasis.WIRE_ID)
        if entry.wire_id is not None:
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
        if entry.record != MAIN_RECORD:
            self._draft(entry, DraftKind.SUBAGENT_TEXT, entry.text)
            return
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
        self._out.closed.append(TurnClosed(turn, reason))


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
