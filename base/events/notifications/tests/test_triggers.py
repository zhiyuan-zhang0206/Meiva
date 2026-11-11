from __future__ import annotations

import pytest

from base.events.notifications.catalog import QUESTION_CATEGORY, UNREAD_CATEGORY
from base.events.notifications.triggers import (
    NotificationAction,
    plan_question_asked,
    plan_question_resolved,
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


def test_a_question_alerts_when_asked_and_retracts_when_resolved() -> None:
    asked = plan_question_asked(question_id=31, title="Deploy now?", agent_label="Builder")
    resolved = plan_question_resolved(question_id=31, title="Deploy now?", agent_label="Builder")
    assert (asked.category, asked.action, asked.tag) == (
        QUESTION_CATEGORY,
        NotificationAction.ALERT,
        "question:31",
    )
    assert (resolved.action, resolved.tag) == (NotificationAction.RETRACT, "question:31")
    assert asked.subject == "Deploy now?"


def test_each_question_has_its_own_identity() -> None:
    first = plan_question_asked(question_id=1, title="A?", agent_label="x")
    second = plan_question_asked(question_id=2, title="B?", agent_label="x")
    assert first.tag != second.tag
