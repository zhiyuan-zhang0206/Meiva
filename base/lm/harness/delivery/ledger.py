from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from base.lm.harness.events import (
    AckSignal,
    BridgeEvent,
    InputInContext,
    InputLost,
    InputReceived,
    InputRejected,
)

# meiva:note
# The one place the three adapters' protocol facts become the unified delivery events, so no adapter
# re-implements the state machine. It is a pure value: every transition returns the next ledger and
# the events that transition announces, and the session applies both. It knows nothing about any
# protocol. It answers four questions the adapters would otherwise each answer their own way:
# what phase an input is in (a phase only moves forward, and a fact that repeats is silent), which
# turn a provider's own turn name is (the attribution table), whether this is the first time a turn
# ended (a turn completes exactly once), and which generation of the process the ledger belongs to
# (a frame from an older generation is not accepted).
# meiva:link base/lm/harness/docs/delivery.md


class InputPhase(StrEnum):
    SENT = "sent"
    RECEIVED = "received"
    IN_CONTEXT = "in_context"
    REJECTED = "rejected"
    LOST = "lost"

    @property
    def settled(self) -> bool:
        return self in (InputPhase.IN_CONTEXT, InputPhase.REJECTED, InputPhase.LOST)


@dataclass(frozen=True, slots=True)
class _Entry:
    phase: InputPhase
    signal: AckSignal | None = None
    turn_id: str | None = None
    # meiva:note
    # The provider's name for the turn the input was steered into: the unit a "no completion event
    # before the turn ended" loss is judged in.
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerStep:
    ledger: DeliveryLedger
    events: tuple[BridgeEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class DeliveryLedger:
    generation: int = 0
    inputs: Mapping[str, _Entry] = field(default_factory=dict[str, _Entry])
    open_turns: Mapping[str, str] = field(default_factory=dict[str, str])
    ended: frozenset[str] = frozenset()

    def next_generation(self) -> DeliveryLedger:
        return replace(self, generation=self.generation + 1, open_turns={})

    def accepts(self, generation: int) -> bool:
        return generation == self.generation

    def phase(self, key: str) -> InputPhase | None:
        entry = self.inputs.get(key)
        return entry.phase if entry is not None else None

    def turn_of(self, key: str) -> str | None:
        entry = self.inputs.get(key)
        return entry.turn_id if entry is not None else None

    def keys_in(self, phase: InputPhase) -> tuple[str, ...]:
        return tuple(key for key, entry in self.inputs.items() if entry.phase is phase)

    def _with(self, key: str, entry: _Entry) -> DeliveryLedger:
        return replace(self, inputs={**self.inputs, key: entry})

    def sent(self, key: str, *, scope: str | None = None) -> DeliveryLedger:
        return self._with(key, _Entry(InputPhase.SENT, scope=scope))

    def scoped(self, key: str, scope: str) -> DeliveryLedger:
        entry = self.inputs.get(key)
        if entry is None or entry.phase.settled:
            return self
        return self._with(key, replace(entry, scope=scope))

    def received(self, key: str, signal: AckSignal) -> LedgerStep:
        entry = self.inputs.get(key)
        if entry is None or entry.phase.settled:
            return LedgerStep(self)
        if entry.phase is InputPhase.RECEIVED and (
            entry.signal is AckSignal.STRONG or signal is AckSignal.WEAK
        ):
            return LedgerStep(self)
        ledger = self._with(key, replace(entry, phase=InputPhase.RECEIVED, signal=signal))
        return LedgerStep(ledger, (InputReceived(key, signal, self.generation),))

    # meiva:note
    # The model has the input. That implies the harness received it, so a strong receipt is announced
    # first when none was: a consumer reading the stream in order never sees an input enter the
    # context that it was never told arrived. A late fact wins over an earlier verdict: an input
    # declared lost or rejected that then turns up in the context is in the context.
    def in_context(self, key: str, turn_id: str) -> LedgerStep:
        entry = self.inputs.get(key)
        if entry is None or entry.phase is InputPhase.IN_CONTEXT:
            return LedgerStep(self)
        step = (
            LedgerStep(self)
            if entry.signal is AckSignal.STRONG
            else self.received(key, AckSignal.STRONG)
        )
        current = step.ledger.inputs.get(key, entry)
        ledger = step.ledger._with(
            key, replace(current, phase=InputPhase.IN_CONTEXT, turn_id=turn_id)
        )
        return LedgerStep(ledger, (*step.events, InputInContext(key, turn_id, self.generation)))

    def rejected(self, key: str, reason: str) -> LedgerStep:
        entry = self.inputs.get(key)
        if entry is None or entry.phase.settled:
            return LedgerStep(self)
        ledger = self._with(key, replace(entry, phase=InputPhase.REJECTED))
        return LedgerStep(ledger, (InputRejected(key, reason, self.generation),))

    def lost(self, key: str) -> LedgerStep:
        entry = self.inputs.get(key)
        if entry is None or entry.phase.settled:
            return LedgerStep(self)
        ledger = self._with(key, replace(entry, phase=InputPhase.LOST))
        return LedgerStep(ledger, (InputLost(key, self.generation),))

    def bind_turn(self, name: str, turn_id: str) -> DeliveryLedger:
        return replace(self, open_turns={**self.open_turns, name: turn_id})

    def local_turn(self, name: str | None) -> str | None:
        return self.open_turns.get(name) if name is not None else None

    # meiva:note
    # A turn completes once. The first call for a name that is open returns its local id and closes
    # it; a repeat, or a name that was never open, returns None so the caller emits no second
    # completion for it.
    def end_turn(self, name: str) -> tuple[DeliveryLedger, str | None]:
        turn_id = self.open_turns.get(name)
        if turn_id is None or turn_id in self.ended:
            return self, None
        remaining = {key: value for key, value in self.open_turns.items() if key != name}
        return replace(self, open_turns=remaining, ended=self.ended | {turn_id}), turn_id

    def lose_unsettled(self, scope: str) -> LedgerStep:
        step = LedgerStep(self)
        for key, entry in self.inputs.items():
            if entry.scope == scope and not entry.phase.settled:
                following = step.ledger.lost(key)
                step = LedgerStep(following.ledger, (*step.events, *following.events))
        return step
