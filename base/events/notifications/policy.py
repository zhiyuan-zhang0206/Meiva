from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from base.events.notifications.catalog import NotificationCategory


class SuppressionReason(StrEnum):
    DISABLED = "disabled"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class DeliveryDecision:
    deliver: bool
    reason: SuppressionReason | None


def enabled_for(category: NotificationCategory, overrides: Mapping[str, bool]) -> bool:
    override = overrides.get(category.id)
    if override is None:
        return category.default_enabled
    if not isinstance(override, bool):
        raise TypeError(f"notification switch for {category.id} must be a bool")
    return override


# meiva:note
# A category is suppressed by the user's switch or by its dedupe window; nothing
# else. A null dedupe key opts out of deduplication entirely, which is how a
# once-per-turn category stays deliverable.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def decide(
    category: NotificationCategory,
    *,
    overrides: Mapping[str, bool],
    last_same_key_at: datetime | None,
    now: datetime,
) -> DeliveryDecision:
    if not enabled_for(category, overrides):
        return DeliveryDecision(deliver=False, reason=SuppressionReason.DISABLED)
    if (
        category.dedupe_window_seconds > 0
        and last_same_key_at is not None
        and (now - last_same_key_at).total_seconds() < category.dedupe_window_seconds
    ):
        return DeliveryDecision(deliver=False, reason=SuppressionReason.DUPLICATE)
    return DeliveryDecision(deliver=True, reason=None)
