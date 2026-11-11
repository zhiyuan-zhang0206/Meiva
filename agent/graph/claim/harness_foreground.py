# meiva:note
# The foreground's bookkeeping as a pure value: what was handed over and what became of it. An input
# handed to an external harness is settled by exactly one fact: it entered the model's context (ACK 2),
# the harness refused it, or the harness declared it lost. A turn ending settles nothing. This module
# folds the harness's events into those facts and says when the graph invocation may end; it does no
# I/O, so every rule is exercised with plain events.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from base.agents.history.timeline_item import TimelineItem
from base.lm.harness import (
    BridgeEvent,
    InputInContext,
    InputLost,
    InputRejected,
    TurnCompleted,
    TurnStarted,
)

__all__ = ["Commit", "Foreground", "Handed", "Requeue", "Settlement"]


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

    @property
    def quiet(self) -> bool:
        return not self.handed and not self.turns

    def hand(self, batch: tuple[Handed, ...]) -> Foreground:
        return replace(self, handed=(*self.handed, *batch))

    # meiva:note
    # Inputs that were certainly never handed over (the session could not take them) leave the unsettled
    # set without a settlement: there is nothing to write for them.
    def withdraw(self, keys: frozenset[str]) -> Foreground:
        return replace(self, handed=tuple(h for h in self.handed if h.key not in keys))

    def on_event(self, event: BridgeEvent) -> tuple[Foreground, tuple[Settlement, ...]]:
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
