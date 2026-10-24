from __future__ import annotations

from ops.pty_close_notices.delivery_plan import DeliveryPlan, Dropped, plan_delivery
from ops.pty_close_notices.notice import STOP_REASON, ClosureNotice, claim_key


def _notice(agent_id: int, session_id: int = 11) -> ClosureNotice:
    return ClosureNotice(
        machine="macmini",
        agent_id=agent_id,
        session_id=session_id,
        name=f"meiva-agent-{agent_id}-shell-{session_id}-report",
        shell_pid=9090,
        shell_birth=f"starttime:{session_id}",
        reason=STOP_REASON,
        closed_at="2026-10-24T00:00:00+00:00",
    )


# meiva:note
# Owners that are running or idling are told, in batch order.
def test_a_live_owner_is_told() -> None:
    running, idling = _notice(1), _notice(2)

    plan = plan_delivery(
        [running, idling],
        owned=frozenset({claim_key(running), claim_key(idling)}),
        statuses={1: "running", 2: "idling"},
    )

    assert plan == DeliveryPlan(deliver=(running, idling), dropped=())


# meiva:note
# A terminated owner and an unknown agent are dropped, not told: a closure notice must never
# resurrect a dead agent.
def test_a_terminated_or_unknown_owner_is_dropped() -> None:
    dead, ghost = _notice(1), _notice(2)

    plan = plan_delivery(
        [dead, ghost],
        owned=frozenset({claim_key(dead), claim_key(ghost)}),
        statuses={1: "terminated"},
    )

    assert plan == DeliveryPlan(
        deliver=(), dropped=(Dropped(dead, "terminated"), Dropped(ghost, None))
    )


# meiva:note
# A key an earlier attempt claimed is skipped entirely, delivered nor dropped, so a re-sent notice
# is never delivered twice.
def test_a_key_another_attempt_claimed_is_skipped() -> None:
    claimed_earlier, fresh = _notice(1, session_id=11), _notice(1, session_id=12)

    plan = plan_delivery(
        [claimed_earlier, fresh],
        owned=frozenset({claim_key(fresh)}),
        statuses={1: "running"},
    )

    assert plan == DeliveryPlan(deliver=(fresh,), dropped=())


# meiva:note
# An unrecognised status is a drop with that status named, never a delivery.
def test_an_owner_in_any_other_status_is_dropped_with_that_status() -> None:
    parked = _notice(1)

    plan = plan_delivery([parked], owned=frozenset({claim_key(parked)}), statuses={1: "paused"})

    assert plan.deliver == ()
    assert plan.dropped == (Dropped(parked, "paused"),)
