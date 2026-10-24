# meiva:note
# The decision to wake a terminated agent, from values: skip or forward, and what follows a spawn.

from __future__ import annotations

from typing import cast

import pytest

from base.agents.messages.inbound import InboundKind, WakeTriggerKind
from base.api_contracts.agent_contract import ResurrectResult
from ops.lifecycle.resurrect_plan import (
    ClearWakeSuppression,
    Forward,
    NotificationOnly,
    RecoveryHalted,
    RereadStatus,
    ResurrectFacts,
    plan_after_result,
    plan_resurrection,
)

_BACKGROUND_KINDS = (
    InboundKind.HEARTBEAT,
    InboundKind.COMPACT_REQUEST,
    InboundKind.SYSTEM_NOTE,
)


def _facts(
    *,
    kind: WakeTriggerKind = InboundKind.CHAT,
    recovery_halted: bool = False,
    notice_source: str | None = None,
    home: str = "runner-a",
    local_machine: str = "gateway-host",
) -> ResurrectFacts:
    return ResurrectFacts(
        agent_id=7,
        trigger_inbound_id=99,
        trigger_kind=kind,
        recovery_halted=recovery_halted,
        notice_source=notice_source,
        home=home,
        local_machine=local_machine,
    )


# meiva:note
# A remote-homed agent is woken on its own machine and never launched from here.
def test_a_remote_home_gets_the_wake_and_no_local_fallback() -> None:
    assert plan_resurrection(_facts()) == Forward(
        home="runner-a",
        path="/api/agents/7/resurrect-if-pending-work",
        trigger_inbound_id=99,
        trigger_inbound_kind=InboundKind.CHAT,
        run_here_if_unreachable=False,
    )


# meiva:note
# Only an agent homed on this machine may fall back to running the wake in this process.
def test_a_local_home_may_run_the_wake_here_when_its_ops_server_is_unreachable() -> None:
    plan = plan_resurrection(_facts(home="gateway-host"))

    assert isinstance(plan, Forward)
    assert plan.run_here_if_unreachable is True


# meiva:note
# A tripped recovery breaker stops every background trigger, whatever else is true of it.
@pytest.mark.parametrize("kind", _BACKGROUND_KINDS)
def test_a_tripped_breaker_stops_background_triggers(kind: WakeTriggerKind) -> None:
    assert plan_resurrection(_facts(kind=kind, recovery_halted=True)) == RecoveryHalted()


# meiva:note
# A fresh chat is an explicit attempt after the fix, so the breaker does not stop it.
def test_a_tripped_breaker_does_not_stop_a_new_chat() -> None:
    assert isinstance(plan_resurrection(_facts(recovery_halted=True)), Forward)


# meiva:note
# Without a tripped breaker a background trigger wakes the agent like any other.
@pytest.mark.parametrize("kind", _BACKGROUND_KINDS)
def test_a_background_trigger_forwards_while_the_breaker_is_clear(kind: WakeTriggerKind) -> None:
    plan = plan_resurrection(_facts(kind=kind))

    assert isinstance(plan, Forward)
    assert plan.trigger_inbound_kind is kind


# meiva:note
# A notification never wakes its owner, and names its source in the plan.
def test_a_system_notice_never_wakes_its_owner() -> None:
    assert plan_resurrection(_facts(notice_source="system:notice-expire")) == NotificationOnly(
        "system:notice-expire"
    )


# meiva:note
# The breaker is judged before the notification rule.
def test_the_breaker_is_judged_before_the_notification_rule() -> None:
    plan = plan_resurrection(
        _facts(kind=InboundKind.HEARTBEAT, recovery_halted=True, notice_source="system")
    )

    assert plan == RecoveryHalted()


# meiva:note
# A kind that cannot trigger a resurrection is refused before anything is sent.
def test_a_kind_that_cannot_wake_an_agent_is_refused() -> None:
    raw_kind = cast(WakeTriggerKind, InboundKind.RESTART)

    with pytest.raises(ValueError, match="cannot trigger resurrection"):
        plan_resurrection(_facts(kind=raw_kind))


# meiva:note
# The suppression a spawn made stale is cleared before the status is read again.
def test_a_spawn_clears_the_suppression_then_rereads_the_status() -> None:
    assert plan_after_result(ResurrectResult.SPAWNED) == (ClearWakeSuppression(), RereadStatus())


# meiva:note
# An agent that was already alive changes nothing.
def test_an_already_alive_agent_needs_nothing_after() -> None:
    assert plan_after_result(ResurrectResult.ALREADY_ALIVE) == ()
