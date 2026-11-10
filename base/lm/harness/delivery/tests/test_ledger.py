from __future__ import annotations

import pytest

from base.lm.harness.delivery import (
    DeliveryLedger,
    HarnessInput,
    ImageBlock,
    InputPhase,
    validated_batch,
)
from base.lm.harness.events import (
    AckSignal,
    InputInContext,
    InputLost,
    InputReceived,
    InputRejected,
)


def ledger(*keys: str) -> DeliveryLedger:
    state = DeliveryLedger().next_generation()
    for key in keys:
        state = state.sent(key)
    return state


def test_receipt_then_context_announces_each_fact_once() -> None:
    step = ledger("a").received("a", AckSignal.WEAK)
    assert step.events == (InputReceived("a", AckSignal.WEAK, 1),)
    assert step.ledger.received("a", AckSignal.WEAK).events == ()
    upgraded = step.ledger.received("a", AckSignal.STRONG)
    assert upgraded.events == (InputReceived("a", AckSignal.STRONG, 1),)
    done = upgraded.ledger.in_context("a", "turn-1")
    assert done.events == (InputInContext("a", "turn-1", 1),)
    assert done.ledger.in_context("a", "turn-1").events == ()
    assert done.ledger.phase("a") is InputPhase.IN_CONTEXT


def test_context_without_a_strong_receipt_announces_the_receipt_first() -> None:
    step = ledger("a").in_context("a", "turn-1")
    assert [type(e) for e in step.events] == [InputReceived, InputInContext]


def test_a_settled_input_cannot_be_rejected_or_lost_but_a_late_fact_wins() -> None:
    settled = ledger("a").in_context("a", "t").ledger
    assert settled.rejected("a", "x").events == ()
    assert settled.lost("a").events == ()
    lost = ledger("b").lost("b").ledger
    assert lost.in_context("b", "t").events[-1] == InputInContext("b", "t", 1)


def test_rejection_and_loss_are_events() -> None:
    assert ledger("a").rejected("a", "no turn").events == (InputRejected("a", "no turn", 1),)
    assert ledger("a").lost("a").events == (InputLost("a", 1),)
    assert ledger().lost("unknown").events == ()


def test_loss_is_judged_per_turn_scope() -> None:
    state = ledger("a", "b", "c").scoped("a", "T1").scoped("b", "T1").scoped("c", "T2")
    state = state.in_context("a", "t").ledger
    step = state.lose_unsettled("T1")
    assert step.events == (InputLost("b", 1),)
    assert step.ledger.phase("c") is InputPhase.SENT


def test_a_turn_ends_once_and_a_new_generation_forgets_open_turns() -> None:
    state = ledger().bind_turn("P", "turn-1")
    state, first = state.end_turn("P")
    assert first == "turn-1"
    assert state.end_turn("P")[1] is None
    reopened = ledger().bind_turn("P", "turn-2").next_generation()
    assert reopened.local_turn("P") is None
    assert not reopened.accepts(1)
    assert reopened.accepts(2)


def test_inputs_are_validated_at_the_boundary() -> None:
    with pytest.raises(ValueError, match="key"):
        _ = HarnessInput("", "x")
    with pytest.raises(ValueError, match="text or an image"):
        _ = HarnessInput("k", " ")
    with pytest.raises(ValueError, match="base64"):
        _ = ImageBlock("image/png", "***")
    with pytest.raises(ValueError, match="unsupported"):
        _ = ImageBlock("image/svg+xml", "aGk=")
    with pytest.raises(ValueError, match="unique"):
        _ = validated_batch([HarnessInput("k", "a"), HarnessInput("k", "b")])
    with pytest.raises(ValueError, match="at least one"):
        _ = validated_batch([])
