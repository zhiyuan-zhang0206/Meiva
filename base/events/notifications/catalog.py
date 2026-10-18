from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Importance = Literal["low", "normal", "high"]
IMPORTANCES: frozenset[str] = frozenset({"low", "normal", "high"})


@dataclass(frozen=True)
class NotificationCategory:
    id: str
    label: str
    description: str
    importance: Importance
    default_enabled: bool
    dedupe_window_seconds: int = 0

    def __post_init__(self) -> None:
        if not self.id or "." not in self.id:
            raise ValueError(f"notification category id must be namespaced: {self.id!r}")
        if self.importance not in IMPORTANCES:
            raise ValueError(f"unknown notification importance: {self.importance!r}")
        if self.dedupe_window_seconds < 0:
            raise ValueError(f"dedupe window must be >= 0: {self.id}")
        if not self.label.strip():
            raise ValueError(f"notification category {self.id} needs a label")


# meiva:note
# Core ids are `core.`-prefixed and a plugin contributes only `<plugin>.` ids, so
# the merged catalog can reject a duplicate id as a programming error rather than
# resolve an ownership clash at delivery time.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
CORE_CATEGORIES: tuple[NotificationCategory, ...] = (
    NotificationCategory(
        id="core.turn_finished",
        label="Agent finished its turn",
        description="An agent settled back to idle after working.",
        importance="normal",
        default_enabled=True,
    ),
    NotificationCategory(
        id="core.needs_input",
        label="Agent is waiting on your answer",
        description="An agent posted a notice that needs a response.",
        importance="high",
        default_enabled=True,
        dedupe_window_seconds=3600,
    ),
    NotificationCategory(
        id="core.notice",
        label="Agent posted a notice",
        description="An agent posted a notice that needs no response.",
        importance="normal",
        default_enabled=False,
        dedupe_window_seconds=3600,
    ),
)


def merge_catalog(
    groups: Iterable[Iterable[NotificationCategory]],
) -> dict[str, NotificationCategory]:
    merged: dict[str, NotificationCategory] = {}
    for group in groups:
        for category in group:
            if category.id in merged:
                raise ValueError(f"notification category declared twice: {category.id}")
            merged[category.id] = category
    return merged


def core_catalog() -> dict[str, NotificationCategory]:
    return {category.id: category for category in CORE_CATEGORIES}
