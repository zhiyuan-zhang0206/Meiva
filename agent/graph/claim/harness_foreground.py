# meiva:note
# The foreground's bookkeeping as a pure value: what was handed over and what became of it. An input
# handed to an external harness is settled by exactly one fact: it entered the model's context (ACK 2),
# the harness refused it, or the harness declared it lost. A turn ending settles nothing. This module
# folds the harness's events into those facts and says when the graph invocation may end; it does no
# I/O, so every rule is exercised with plain events.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType

from agent.graph.claim.harness_render import fit_input_images, skipped_images_note
from base.agents.history.timeline_item import TimelineItem
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    ImageBlock,
    InputInContext,
    InputLost,
    InputReceived,
    InputRejected,
    InterruptConfirmed,
    Reconciliation,
    TurnCompleted,
    TurnStarted,
)

__all__ = [
    "Commit",
    "Foreground",
    "Handed",
    "Reconciled",
    "Requeue",
    "Settlement",
    "event_generation",
    "plan_reconciliation",
]


# meiva:note
# One input handed to the harness and not yet settled. `inbound_id` names its inbox row; a synthetic
# input (a continuation note, a fork marker, a visibility correction) has no row and `None` there.
# One wake's batch is one input: `inbound_id` is the row that names it (the first that is not passive)
# and `carried` the other rows that ride in its text, settled with it by the one acknowledgement.
# `items` are the timeline entries of all its rows, written when the input enters the context, never
# before.
# meiva:link agent/graph/docs/harness-turn.md
@dataclass(frozen=True, slots=True)
class Handed:
    key: str
    inbound_id: int | None
    items: tuple[TimelineItem, ...]
    human: bool = False
    carried: tuple[int, ...] = ()

    @property
    def row_ids(self) -> tuple[int, ...]:
        return (*(() if self.inbound_id is None else (self.inbound_id,)), *self.carried)


# meiva:note
# The rows of one wake's batch as one input: the first row that is not passive names it (a batch of
# passive rows alone is never handed over), the others ride in its text and settle with it. Their
# timeline entries stay in queue order.
def merge_handed(parts: Sequence[tuple[Handed, bool]]) -> Handed:
    primary = next((h for h, passive in parts if not passive), parts[0][0])
    return Handed(
        primary.key,
        primary.inbound_id,
        tuple(item for h, _ in parts for item in h.items),
        any(h.human for h, _ in parts),
        tuple(i for h, _ in parts for i in h.row_ids if i != primary.inbound_id),
    )


@dataclass(frozen=True, slots=True)
class Commit:
    handed: Handed
    turn_id: str


@dataclass(frozen=True, slots=True)
class Requeue:
    handed: Handed
    reason: str


Settlement = Commit | Requeue


# meiva:note
# `handed` keeps delivery order. `turns` are the turns the harness is running, whoever began them. The
# invocation may end only when both are empty: an input that missed a turn's last boundary is settled
# after that turn ended (Claude Code runs it as a turn of its own about a second later), and ending
# earlier would leave that settlement with nobody to write it.
@dataclass(frozen=True, slots=True)
class Foreground:
    handed: tuple[Handed, ...] = ()
    turns: frozenset[str] = frozenset()
    generation: int = 0

    @property
    def quiet(self) -> bool:
        return not self.handed and not self.turns

    def hand(self, batch: tuple[Handed, ...]) -> Foreground:
        return replace(self, handed=(*self.handed, *batch))

    # meiva:note
    # A frame of an earlier process than the one this foreground reads. Turn names restart with every
    # session and input keys are reused by a re-delivery, so such a frame could end the wrong turn or settle
    # a re-delivered input: it is dropped before anything reads it. A frame that carries no generation
    # (0) is not judged.
    def is_stale(self, event: BridgeEvent) -> bool:
        generation = event_generation(event)
        return generation is not None and 0 < generation < self.generation

    # meiva:note
    # Inputs that were certainly never handed over (the session could not take them) leave the unsettled
    # set without a settlement: there is nothing to write for them.
    def withdraw(self, keys: frozenset[str]) -> Foreground:
        return replace(self, handed=tuple(h for h in self.handed if h.key not in keys))

    def on_event(self, event: BridgeEvent) -> tuple[Foreground, tuple[Settlement, ...]]:
        if self.is_stale(event):
            return self, ()
        match event:
            case TurnStarted(turn_id=turn_id):
                return replace(self, turns=self.turns | {turn_id}), ()
            case TurnCompleted(turn_id=turn_id):
                return replace(self, turns=self.turns - {turn_id}), ()
            case InputInContext(key=key, turn_id=turn_id):
                return self._settle(key, lambda h: Commit(h, turn_id))
            case InputRejected(key=key, reason=reason):
                return self._settle(key, lambda h: Requeue(h, reason))
            case InputLost(key=key):
                return self._settle(key, lambda h: Requeue(h, "the harness lost the input"))
            case _:
                return self, ()

    def _settle(
        self, key: str, make: Callable[[Handed], Settlement]
    ) -> tuple[Foreground, tuple[Settlement, ...]]:
        found = next((h for h in self.handed if h.key == key), None)
        if found is None:
            return self, ()
        settlement = make(found)
        rest = tuple(h for h in self.handed if h.key != key)
        return replace(self, handed=rest), (settlement,)


# meiva:note
# The generation a delivery event carries, or None for an event that names none (activity text, tool
# calls). A turn's completion that an adapter does not stamp yet carries 0.
def event_generation(event: BridgeEvent) -> int | None:
    match event:
        case (
            InputReceived(generation=generation)
            | InputInContext(generation=generation)
            | InputRejected(generation=generation)
            | InputLost(generation=generation)
            | TurnStarted(generation=generation)
            | TurnCompleted(generation=generation)
            | InterruptConfirmed(generation=generation)
        ):
            return generation
        case _:
            return None


# meiva:note
# What reconciling the unsettled inputs against the harness's durable record decided, per input and in
# the order they were handed over. `commit`: the record holds it in the model's context, so it settles
# like an acknowledgement. `requeue`: the record has no trace of it, so it goes back to the queue at its
# original position and is delivered again, the only case that may deliver twice. `keep`: the harness
# stored it and delivers it by itself, so it stays claimed and its later facts settle it. `unknown`: the
# record cannot tell (Antigravity records only steered inputs), so it is consumed with the marker.
@dataclass(frozen=True, slots=True)
class Reconciled:
    commit: tuple[Handed, ...] = ()
    requeue: tuple[Handed, ...] = ()
    keep: tuple[Handed, ...] = ()
    unknown: tuple[Handed, ...] = ()


def plan_reconciliation(
    handed: Sequence[Handed], answers: Mapping[str, Reconciliation]
) -> Reconciled:
    missing = [h.key for h in handed if h.key not in answers]
    if missing:
        raise ValueError(f"the harness gave no reconciliation answer for {missing}")
    commit: list[Handed] = []
    requeue: list[Handed] = []
    keep: list[Handed] = []
    unknown: list[Handed] = []
    for entry in handed:
        match answers[entry.key]:
            case Reconciliation.IN_CONTEXT:
                commit.append(entry)
            case Reconciliation.NOT_FOUND:
                requeue.append(entry)
            case Reconciliation.PERSISTED_UNDELIVERED:
                keep.append(entry)
            case Reconciliation.UNKNOWN:
                unknown.append(entry)
    return Reconciled(tuple(commit), tuple(requeue), tuple(keep), tuple(unknown))


# meiva:note
# One wake's rows and the pre-turn hooks' note are one input, so the model's first request holds all of
# them: each row's text in queue order, then the note, separated by blank lines (a native request
# carries the same messages in one call). Handing them over one by one made the first input open the
# turn alone and every other one run as a turn of its own about a second later (Claude Code), or reach
# the model only after its first request had gone out (Codex, Antigravity): the agent first answered a
# passive note or a hook note and only then the person. The rows keep their own timeline entries and
# settle together with the one acknowledgement.
#
# What one reservation hands the harness: always one input. The synthetic inputs' text comes first (a
# restart or resume note, a context reset, a continuation: system hints the model needs before the
# messages), then each row in queue order, then the hook note (a native request carries hook messages
# after the messages), separated by blank lines. The synthetic inputs ride in it with their timeline
# entries and settle with it. A row with no text settles at once. A wake with row text is named by its
# first row that is not passive, and `folded` is what to hand back if the input is refused, lost or not
# taken. A wake with no row text is named by its first synthetic input and is itself a synthetic input
# (no row names it), so a refusal hands it back whole, text and timeline entries, and `folded` is
# empty; `prompts` is the text each synthetic input is handed back under. `group` is the merged input
# when it carries several rows, which is what must be marked so a crash can rebuild it. The images of
# the input are bounded together, not per row.
# meiva:link agent/graph/docs/harness-turn.md
@dataclass(frozen=True, slots=True)
class WakePlan:
    handed: tuple[Handed, ...]
    inputs: tuple[HarnessInput, ...]
    settled_now: tuple[Handed, ...]
    group: Handed | None
    folded: tuple[tuple[Handed, str], ...] = ()
    prompts: Mapping[str, str] = MappingProxyType({})


def plan_wake(
    synthetic: Sequence[tuple[Handed, str]],
    rows: Sequence[tuple[Handed, str, bool]],
    images: Mapping[str, tuple[ImageBlock, ...]],
    *,
    hook_notes_key: str,
) -> WakePlan:
    parts = [(entry, text, passive) for entry, text, passive in rows if text.strip()]
    settled_now = tuple(entry for entry, text, _ in rows if not text.strip())
    prompts = {entry.key: prompt for entry, prompt in synthetic}
    ordered = _in_wake_order(synthetic, parts, hook_notes_key)
    if not ordered:
        return WakePlan((), (), settled_now, None)
    wake = merge_handed([(entry, passive) for entry, _, passive in ordered])
    wake_input = _wake_input(wake, ordered, images)
    if not parts:
        return WakePlan(
            (wake,),
            (wake_input,),
            settled_now,
            None,
            prompts={**prompts, wake.key: wake_input.text},
        )
    return WakePlan(
        (wake,),
        (wake_input,),
        settled_now,
        wake if wake.carried else None,
        tuple(synthetic),
        prompts,
    )


def _in_wake_order(
    synthetic: Sequence[tuple[Handed, str]],
    parts: Sequence[tuple[Handed, str, bool]],
    hook_notes_key: str,
) -> list[tuple[Handed, str, bool]]:
    notes = [(e, p, True) for e, p in synthetic if e.key.startswith(hook_notes_key)]
    hints = [(e, p, True) for e, p in synthetic if not e.key.startswith(hook_notes_key)]
    return [*hints, *parts, *notes]


def _wake_input(
    wake: Handed,
    parts: Sequence[tuple[Handed, str, bool]],
    images: Mapping[str, tuple[ImageBlock, ...]],
) -> HarnessInput:
    text = "\n\n".join(text for _, text, _ in parts)
    kept, skipped = fit_input_images(
        [image for entry, _, _ in parts for image in images.get(entry.key, ())]
    )
    return HarnessInput(
        key=wake.key,
        text=f"{text}\n\n{skipped_images_note(skipped)}" if skipped else text,
        images=kept,
    )
