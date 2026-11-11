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

from base.agents.history.timeline_item import TimelineItem
from base.lm.harness import (
    BridgeEvent,
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
# `items` are the timeline entries written when the input enters the context, never before.
# meiva:link agent/graph/docs/harness-turn.md
@dataclass(frozen=True, slots=True)
class Handed:
    key: str
    inbound_id: int | None
    items: tuple[TimelineItem, ...]
    human: bool = False


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
