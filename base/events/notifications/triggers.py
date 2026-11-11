from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from base.events.notifications.catalog import QUESTION_CATEGORY, UNREAD_CATEGORY
from base.events.notifications.preview import unread_preview


class NotificationAction(StrEnum):
    ALERT = "alert"
    REPLACE = "replace"
    RETRACT = "retract"


# meiva:note
# One push as the producers decide it, before it is a row: the category it belongs to, what the
# client does with it (raise it with sound, swap the content of the one already shown without a
# sound, or take it away) and the stable `tag` that names which on-screen notification it is.
# A client keeps at most one notification per tag, so `tag` is the whole replace/retract contract.
# meiva:link base/events/notifications/README.md
@dataclass(frozen=True)
class PushPlan:
    category: str
    action: NotificationAction
    tag: str
    subject: str
    body: str | None


def _unread_tag(agent_id: int) -> str:
    return f"unread:{agent_id}"


# meiva:note
# The unread state is the seen cursor, nothing stored beside it. A delivery written while the user
# had nothing unread starts a new round and alerts once; a delivery written while an unread push is
# already showing only swaps that push's content. `had_unread` is the roster's own definition,
# read before the new delivery is inserted.
# meiva:link base/events/notifications/README.md
def plan_unread_delivery(
    *, agent_label: str, content: str, agent_id: int, had_unread: bool
) -> PushPlan:
    return PushPlan(
        category=UNREAD_CATEGORY,
        action=NotificationAction.REPLACE if had_unread else NotificationAction.ALERT,
        tag=_unread_tag(agent_id),
        subject=agent_label,
        body=unread_preview(content),
    )


# meiva:note
# A cursor move retracts the push only when it is the move that clears the last unread delivery:
# unread before, nothing unread after. A move that leaves newer deliveries unread keeps the push,
# and a move with nothing outstanding has nothing to take away.
# meiva:link base/events/notifications/README.md
def plan_unread_seen(
    *, agent_label: str, agent_id: int, had_unread: bool, has_unread: bool
) -> PushPlan | None:
    if not had_unread or has_unread:
        return None
    return PushPlan(
        category=UNREAD_CATEGORY,
        action=NotificationAction.RETRACT,
        tag=_unread_tag(agent_id),
        subject=agent_label,
        body=None,
    )


def _questions_tag(agent_id: int) -> str:
    return f"question:agent:{agent_id}"


def _questions_subject(open_count: int, first_title: str) -> str:
    return first_title if open_count == 1 else f"{open_count} questions awaiting an answer"


# meiva:note
# One push per agent, not per question: the first open question alerts, every later one only swaps
# the content of the push already showing (silent), and the push is retracted when the agent has
# no open question left. A batch of questions in one ask is one event of this kind.
# meiva:link base/events/notifications/README.md
def plan_questions_open(
    *, agent_id: int, agent_label: str, open_count: int, first_title: str, had_open: bool
) -> PushPlan:
    return PushPlan(
        category=QUESTION_CATEGORY,
        action=NotificationAction.REPLACE if had_open else NotificationAction.ALERT,
        tag=_questions_tag(agent_id),
        subject=_questions_subject(open_count, first_title),
        body=agent_label,
    )


def plan_questions_cleared(*, agent_id: int, agent_label: str) -> PushPlan:
    return PushPlan(
        category=QUESTION_CATEGORY,
        action=NotificationAction.RETRACT,
        tag=_questions_tag(agent_id),
        subject="Questions answered",
        body=agent_label,
    )
