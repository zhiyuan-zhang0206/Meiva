# meiva:note
# What one read of the lease means, as values: which rows are due, which the merge window may
# hold, and whether the relay goes on, stops quietly or words the end.

from __future__ import annotations

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from cli.commands.agents.relay.inbox import (
    EndedLease,
    EndVerdict,
    InboxSnapshot,
    LeaseRef,
    due_ids,
    end_verdict,
    routine_ids,
    terminal_record,
)
from cli.commands.agents.tests.relay.relay_inputs import NOW, message, snapshot

OPEN = (
    ImpersonationStatus.REQUESTED,
    ImpersonationStatus.ACCEPTED,
    ImpersonationStatus.ACTIVE,
)


@pytest.mark.parametrize("notify_terminal", [True, False])
@pytest.mark.parametrize("status", OPEN)
def test_an_open_lease_goes_on(status: ImpersonationStatus, notify_terminal: bool) -> None:
    assert end_verdict(status, notify_terminal=notify_terminal) is EndVerdict.CONTINUE


@pytest.mark.parametrize("status", list(ImpersonationStatus))
def test_a_host_that_submits_its_own_notice_is_never_told_twice(
    status: ImpersonationStatus,
) -> None:
    verdict = end_verdict(status, notify_terminal=False)
    assert verdict is (EndVerdict.CONTINUE if status in OPEN else EndVerdict.STOP_SILENT)


def test_the_releasing_controller_already_knows_and_is_not_told() -> None:
    assert end_verdict(ImpersonationStatus.RELEASED, notify_terminal=True) is EndVerdict.STOP_SILENT


@pytest.mark.parametrize("status", [ImpersonationStatus.REJECTED, ImpersonationStatus.EXPIRED])
def test_a_controller_session_is_told_how_its_lease_ended(status: ImpersonationStatus) -> None:
    assert end_verdict(status, notify_terminal=True) is EndVerdict.STOP_WITH_NOTICE


def test_user_chats_cancels_and_reminders_are_never_routine() -> None:
    rows = (
        message(1),
        message(2, kind="chat", source="user"),
        message(3, kind="chat", source="agent:7"),
        message(4, kind="cancel"),
        message(5, kind="reminder"),
        message(6, kind="watcher_wake", source="watcher:3"),
    )
    assert routine_ids(rows) == {1, 3, 6}


def test_due_rows_are_first_attempts_or_retries_with_budget_left_and_a_closed_ack_window() -> None:
    page = snapshot(
        message(1),
        message(2, attempts=1, due=True),
        message(3, attempts=1, due=False),
        message(4, due=False),
        message(5, attempts=2, due=True),
        max_attempts=2,
    )
    assert due_ids(page, redelivery=False) == {1}
    assert due_ids(page, redelivery=True) == {2}


def test_a_snapshot_names_its_rows_and_hands_one_over_by_id() -> None:
    page = snapshot(message(7, content="the body rides into the envelope"), message(9))
    assert page.message_ids == {7, 9}
    assert page.message(7).content == "the body rides into the envelope"
    with pytest.raises(ValueError, match="not in this snapshot"):
        page.message(8)


def test_only_an_active_lease_exposes_an_inbox() -> None:
    closed = InboxSnapshot.without_inbox(
        ImpersonationStatus.EXPIRED,
        NOW,
        end_reason="confirmed executor death",
        ended=None,
        budget=snapshot().budget,
    )
    assert not closed.active
    assert closed.message_ids == frozenset()
    assert closed.end_reason == "confirmed executor death"
    assert snapshot().active


def test_the_terminal_record_keeps_its_six_facts_and_ignores_its_other_readers() -> None:
    record = EndedLease.model_validate(
        {
            "session_id": 3,
            "lease_id": "lease-id",
            "agent_id": 42,
            "status": "expired",
            "reason": None,
            "ended_at": "2026-10-05T10:00:00+00:00",
            "provider": "codex",
            "thread_id": "t",
            "endpoint": "unix:///x",
        }
    )
    assert record.model_dump() == {
        "session_id": 3,
        "lease_id": "lease-id",
        "agent_id": 42,
        "status": "expired",
        "reason": None,
        "ended_at": "2026-10-05T10:00:00+00:00",
    }


def test_a_terminal_record_missing_a_fact_is_refused_where_it_is_read() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        EndedLease.model_validate(
            {"session_id": 3, "lease_id": "x", "status": "expired", "reason": None, "ended_at": "t"}
        )


def test_a_lease_without_a_terminal_record_is_described_by_the_supplied_scope() -> None:
    page = snapshot(status=ImpersonationStatus.REJECTED, end_reason="relay would not start")
    assert terminal_record(page, LeaseRef(agent_id=42, lease_ref=3)) == EndedLease(
        session_id=3,
        lease_id="3",
        agent_id=42,
        status="rejected",
        reason="relay would not start",
        ended_at="unknown (legacy record)",
    )


def test_a_lease_with_a_terminal_record_is_described_by_that_record() -> None:
    record = EndedLease(
        session_id=0,
        lease_id="lease",
        agent_id=42,
        status="expired",
        reason=None,
        ended_at="2026-10-05T10:00:00+00:00",
    )
    page = snapshot(status=ImpersonationStatus.EXPIRED, ended=record)
    assert terminal_record(page, LeaseRef(agent_id=999, lease_ref=1)) is record
