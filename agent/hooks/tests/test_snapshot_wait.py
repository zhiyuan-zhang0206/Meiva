from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage

from agent.hooks.snapshot_wait import SnapshotWait, read_time, snapshot_verdict

_LAST = datetime(2026, 10, 7, 12, 0, 10, tzinfo=UTC)
_WAIT = SnapshotWait(wait_seconds=5.0, poll_seconds=0.5)


def _at(second: int) -> datetime:
    return datetime(2026, 10, 7, 12, 0, second, tzinfo=UTC)


def test_the_defaults_are_the_tuned_wait_and_poll() -> None:
    assert (SnapshotWait().wait_seconds, SnapshotWait().poll_seconds) == (5.0, 0.5)


def test_a_checkpoint_as_new_as_the_last_message_holds_it() -> None:
    assert snapshot_verdict(_at(10), _LAST, 0.0, _WAIT) == "caught_up"
    assert snapshot_verdict(_at(20), _LAST, 4.9, _WAIT) == "caught_up"


def test_an_older_or_missing_checkpoint_is_waited_for_until_the_wait_is_spent() -> None:
    for newest in (_at(9), None):
        assert snapshot_verdict(newest, _LAST, 0.0, _WAIT) == "wait"
        assert snapshot_verdict(newest, _LAST, 4.99, _WAIT) == "wait"
        assert snapshot_verdict(newest, _LAST, 5.0, _WAIT) == "give_up"


def test_a_caught_up_checkpoint_wins_over_a_spent_wait() -> None:
    assert snapshot_verdict(_at(11), _LAST, 60.0, _WAIT) == "caught_up"


def _stamped(stamp: str | None, **extra: str) -> HumanMessage:
    kwargs = {} if stamp is None else {"meiva_created_at": stamp}
    return HumanMessage(content="m", id="m", additional_kwargs={**kwargs, **extra})


def test_the_read_time_of_a_created_message_is_its_creation() -> None:
    assert read_time(_stamped("2026-10-07T12:00:10+00:00")) == _LAST


def test_an_inbound_message_is_read_when_it_was_picked_up() -> None:
    inbound = _stamped(
        "2026-10-07T12:00:03+00:00",
        meiva_msg_type="inbound",
        meiva_picked_up_at="2026-10-07T12:00:10+00:00",
    )

    assert read_time(inbound) == _LAST


@pytest.mark.parametrize(
    "stamp",
    [None, "2026-10-07T12:00:10", "not a time"],
    ids=["no timestamp", "no timezone", "unparsable"],
)
def test_a_message_without_a_usable_time_has_no_read_time(stamp: str | None) -> None:
    assert read_time(_stamped(stamp)) is None
