from __future__ import annotations

import pytest

from base.events.notifications.catalog import QUESTION_CATEGORY, UNREAD_CATEGORY
from base.events.notifications.triggers import (
    NotificationAction,
    plan_questions_cleared,
    plan_questions_open,
    plan_unread_delivery,
    plan_unread_seen,
)


def _delivery(*, had_unread: bool, content: str = "Build finished") -> NotificationAction:
    return plan_unread_delivery(
        agent_label="Builder", content=content, agent_id=7, had_unread=had_unread
    ).action


def test_the_first_delivery_after_everything_was_read_alerts() -> None:
    assert _delivery(had_unread=False) is NotificationAction.ALERT


def test_a_delivery_while_one_is_already_unread_replaces_silently() -> None:
    assert _delivery(had_unread=True) is NotificationAction.REPLACE


def test_an_unread_push_is_one_per_agent_and_carries_the_latest_output() -> None:
    first = plan_unread_delivery(
        agent_label="Builder", content="step one", agent_id=7, had_unread=False
    )
    second = plan_unread_delivery(
        agent_label="Builder", content="step two", agent_id=7, had_unread=True
    )
    other = plan_unread_delivery(
        agent_label="Other", content="step one", agent_id=8, had_unread=False
    )
    assert first.tag == second.tag == "unread:7"
    assert other.tag == "unread:8"
    assert (second.category, second.subject, second.body) == (
        UNREAD_CATEGORY,
        "Builder",
        "step two",
    )


def test_catching_the_cursor_up_to_the_last_unread_retracts() -> None:
    plan = plan_unread_seen(agent_label="Builder", agent_id=7, had_unread=True, has_unread=False)
    assert plan is not None
    assert (plan.action, plan.tag) == (NotificationAction.RETRACT, "unread:7")


@pytest.mark.parametrize(
    ("had_unread", "has_unread"),
    [(True, True), (False, False), (False, True)],
    ids=["newer delivery still unread", "nothing was outstanding", "reading cannot add unread"],
)
def test_a_cursor_move_that_clears_nothing_retracts_nothing(
    had_unread: bool, has_unread: bool
) -> None:
    assert (
        plan_unread_seen(
            agent_label="Builder", agent_id=7, had_unread=had_unread, has_unread=has_unread
        )
        is None
    )


def test_the_first_open_question_alerts_later_ones_replace_silently_and_the_end_retracts() -> None:
    first = plan_questions_open(
        agent_id=7, agent_label="Builder", open_count=1, first_title="Deploy now?", had_open=False
    )
    more = plan_questions_open(
        agent_id=7, agent_label="Builder", open_count=3, first_title="Deploy now?", had_open=True
    )
    cleared = plan_questions_cleared(agent_id=7, agent_label="Builder")
    assert (first.category, first.action, first.tag) == (
        QUESTION_CATEGORY,
        NotificationAction.ALERT,
        "question:agent:7",
    )
    assert first.subject == "Deploy now?"
    assert (more.action, more.tag, more.subject) == (
        NotificationAction.REPLACE,
        "question:agent:7",
        "3 questions awaiting an answer",
    )
    assert (cleared.action, cleared.tag) == (NotificationAction.RETRACT, "question:agent:7")


def test_each_agent_has_its_own_question_push() -> None:
    one = plan_questions_open(
        agent_id=1, agent_label="x", open_count=1, first_title="A?", had_open=False
    )
    two = plan_questions_open(
        agent_id=2, agent_label="x", open_count=1, first_title="A?", had_open=False
    )
    assert one.tag != two.tag
