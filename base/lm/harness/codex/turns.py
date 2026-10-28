from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from base.lm.harness.codex.frames import JsonObject, JsonValue, RequestParams, as_object, as_text
from base.lm.harness.codex.mapping import TurnFields, assistant_text
from base.lm.harness.events import TurnCompleted, Usage
from base.lm.harness.turn import SessionTotals, TurnOutcome


class NotificationKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    ITEM_STARTED = "item_started"
    ITEM_COMPLETED = "item_completed"
    USAGE = "usage"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    ERROR = "error"


# meiva:note
# Every notification the bridge reads, by the method that carries it, and nothing for the rest:
# reasoning, plan, image, sub-agent, queue and rate-limit events map to no kind because the runtime
# does not model them.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
_KINDS = {
    "item/agentMessage/delta": NotificationKind.TEXT_DELTA,
    "item/reasoning/summaryTextDelta": NotificationKind.REASONING_DELTA,
    "item/reasoning/textDelta": NotificationKind.REASONING_DELTA,
    "item/started": NotificationKind.ITEM_STARTED,
    "item/completed": NotificationKind.ITEM_COMPLETED,
    "thread/tokenUsage/updated": NotificationKind.USAGE,
    "turn/started": NotificationKind.TURN_STARTED,
    "turn/completed": NotificationKind.TURN_COMPLETED,
    "error": NotificationKind.ERROR,
}


# meiva:note
# A notification this session is going to act on: what it is, which provider turn it reports on,
# and the completed turn it carries. `turn_id` is the name the message itself gives — `turnId` for
# a turn-scoped notification, the `turn` object's own id for `turn/completed` — so a message that
# names a turn this session never bound has an owner of its own and no ticket adopts it (#5663).
# An empty `turn` is every kind that carries no completed turn.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
@dataclass(frozen=True, slots=True)
class Inbound:
    kind: NotificationKind
    turn_id: str | None
    turn: JsonObject = field(default_factory=dict)


# meiva:note
# What an `item/*` notification asks for: the phase to report it in, and the reply an
# `item/completed` carries — `final` when the item is the turn's final answer rather than
# commentary, which is what a settled turn prefers as its text.
@dataclass(frozen=True, slots=True)
class ItemStep:
    phase: str
    text: str | None = None
    final: bool = False


# meiva:note
# A provider turn name this session may adopt: the name itself, and whether adopting it discards
# the partial state gathered under an older name — a name that changed mid-turn is the provider
# replaying another turn's transcript, and what was gathered under the old name is not ours
# (#5663).
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
@dataclass(frozen=True, slots=True)
class Bind:
    name: str
    discard: bool


# meiva:note
# An error the server will not retry: its text, and the turn it names (None when it names none).
@dataclass(frozen=True, slots=True)
class ErrorReport:
    text: str
    turn_id: str | None


# meiva:note
# Everything one turn of this session has produced so far: the usage it reported step by step,
# the reply parts it streamed and the ones that were its final answer, the denials it collected,
# and whether an interrupt was asked for. The session holds one of these per running turn and
# settles the turn from it, so an incoming message changes a value instead of three maps.
@dataclass(frozen=True, slots=True)
class TurnRecord:
    usage: Usage = field(default_factory=Usage)
    steps: tuple[Usage, ...] = ()
    parts: tuple[str, ...] = ()
    finals: tuple[str, ...] = ()
    denials: tuple[str, ...] = ()
    interrupt_requested: bool = False

    # meiva:note
    # A token report of nothing says nothing: `thread/tokenUsage/updated` also arrives for a turn
    # that has not spent tokens yet, and counting it as a step would report a turn that did not run.
    def with_usage(self, step: Usage) -> TurnRecord:
        if step.input_tokens == 0 and step.output_tokens == 0:
            return self
        return replace(self, usage=self.usage + step, steps=(*self.steps, step))

    def with_text(self, text: str, *, final: bool) -> TurnRecord:
        return replace(
            self,
            parts=(*self.parts, text),
            finals=(*self.finals, text) if final else self.finals,
        )

    def with_denial(self, denial: str) -> TurnRecord:
        return replace(self, denials=(*self.denials, denial))

    def interrupted(self) -> TurnRecord:
        return replace(self, interrupt_requested=True)

    # meiva:note
    # The reply a settled turn carries: its final answers when it named any, else everything it
    # streamed — one long reply can arrive in several `agentMessage` items.
    @property
    def text(self) -> str:
        return "\n".join(self.finals or self.parts).strip()


# meiva:note
# What a `turn/completed` naming our turn settles it with: the outcome the caller awaits, the event
# that records it, and the session totals after the turn.
@dataclass(frozen=True, slots=True)
class Settlement:
    outcome: TurnOutcome
    completed: TurnCompleted
    totals: SessionTotals


# meiva:note
# One app-server connection serves one thread, and a notification naming another one belongs to work
# this session never asked for: a fork, or a thread the CLI opened for itself. The protocol puts
# `threadId` on every turn-scoped notification, so the check is one look-up (#5663).
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
def inbound(method: str, params: RequestParams, *, session_id: str | None) -> Inbound | None:
    kind = _KINDS.get(method)
    if kind is None:
        return None
    thread_id = as_text(params.get("threadId"))
    if thread_id is not None and session_id is not None and thread_id != session_id:
        return None
    if kind is NotificationKind.TURN_COMPLETED:
        turn = as_object(params.get("turn"))
        return Inbound(kind, as_text(turn.get("id")) or "", turn)
    return Inbound(kind, as_text(params.get("turnId")))


def item(kind: NotificationKind, params: RequestParams) -> ItemStep:
    completed = kind is NotificationKind.ITEM_COMPLETED
    payload = as_object(params.get("item"))
    text = assistant_text(payload) if completed else None
    return ItemStep(
        phase="completed" if completed else "started",
        text=text,
        final=text is not None and assistant_text(payload, final_only=True) is not None,
    )


# meiva:note
# `turn/started` names the turn the server is about to run, and it can arrive before `turn/start`
# has answered. Binding that name is what lets the items that follow be attributed without
# guessing: only a turn this session is in the middle of starting may take a name, so a name
# announced at any other moment belongs to a turn we did not start (#5663).
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
def binding(container: RequestParams, *, previous: str | None, starting: bool) -> Bind | None:
    if not starting:
        return None
    name = as_text(as_object(container.get("turn")).get("id"))
    if not name:
        return None
    return Bind(name=name, discard=previous is not None and previous != name)


# meiva:note
# An error the server will not retry, on a turn this session is not running, is news about the
# provider rather than a verdict on our turn: the caller routes it to the sink instead of ending a
# wait it has nothing to do with (#5663).
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
def error_report(params: RequestParams) -> ErrorReport | None:
    if bool(params.get("willRetry", False)):
        return None
    message = str(as_object(params.get("error")).get("message") or "app-server reported an error")
    return ErrorReport(text=message, turn_id=as_text(params.get("turnId")))


# meiva:note
# The reply a completed turn carries, read out for a turn this session did not run: it is content
# nobody here has read, and the only part of such a turn worth handing on (#5663).
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
def unclaimed_text(turn: RequestParams) -> str:
    items: JsonValue = turn.get("items")
    if not isinstance(items, list):
        return ""
    return "\n".join(
        text for entry in items if (text := assistant_text(as_object(entry))) is not None
    ).strip()


# meiva:note
# The turn boundary: the status the server reports settles the outcome, the interrupt flag adds the
# one the server never sees, and the usage the turn streamed is what it spent. `steps` counts the
# reports it sent, so a provider that reports once and a provider that reports per tool call both
# name their own number of steps.
def settle(
    *,
    turn_id: str,
    session_id: str | None,
    turn: RequestParams,
    record: TurnRecord,
    totals: SessionTotals,
) -> Settlement:
    fields = TurnFields.parse(turn)
    usage = record.usage
    steps = record.steps
    num_turns = max(len(steps), 1)
    text = record.text
    interrupted = record.interrupt_requested or fields.status == "interrupted"
    is_error = fields.status in {"failed", "interrupted"} or fields.error_message is not None
    outcome = TurnOutcome(
        turn_id=turn_id,
        text=text,
        subtype=fields.status,
        is_error=is_error,
        interrupted=interrupted,
        stop_reason=fields.error_message,
        usage=usage,
        cost_usd=None,
        permission_denials=record.denials,
        num_turns=num_turns,
        steps=steps,
    )
    completed = TurnCompleted(
        turn_id=turn_id,
        session_id=session_id,
        text=text,
        subtype=fields.status,
        is_error=is_error,
        interrupted=interrupted,
        stop_reason=fields.error_message,
        num_turns=num_turns,
        duration_ms=fields.duration_ms,
        usage=usage,
        cost_usd=None,
        cumulative_cost_usd=None,
        permission_denials=record.denials,
        steps=steps,
    )
    return Settlement(outcome=outcome, completed=completed, totals=totals.with_turn(usage, None))
