# meiva:note
# The system push has exactly two kinds of event: an agent asked the user a question, and an
# agent has output the user has not read. Anything else the user may want to know belongs in the
# log, or the timeline that already carries it, never in this catalog. Both are always on: no
# user setting suppresses either.
# meiva:link base/events/notifications/README.md

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Importance = Literal["low", "normal", "high"]

QUESTION_CATEGORY = "core.question"
UNREAD_CATEGORY = "core.unread"


@dataclass(frozen=True)
class NotificationCategory:
    id: str
    importance: Importance


CORE_CATEGORIES: tuple[NotificationCategory, ...] = (
    NotificationCategory(
        id=QUESTION_CATEGORY,
        importance="high",
    ),
    NotificationCategory(
        id=UNREAD_CATEGORY,
        importance="normal",
    ),
)


def core_catalog() -> dict[str, NotificationCategory]:
    return {category.id: category for category in CORE_CATEGORIES}


def category(category_id: str) -> NotificationCategory:
    declared = core_catalog().get(category_id)
    if declared is None:
        raise ValueError(f"unknown notification category: {category_id}")
    return declared
