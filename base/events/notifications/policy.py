from __future__ import annotations

from collections.abc import Mapping

from base.events.notifications.catalog import NotificationCategory


# meiva:note
# The user's per-category switch is the only suppression there is: a category without an override
# ships at its declared default.
# meiva:link base/events/notifications/README.md
def enabled_for(category: NotificationCategory, overrides: Mapping[str, bool]) -> bool:
    override = overrides.get(category.id)
    if override is None:
        return category.default_enabled
    if not isinstance(override, bool):
        raise TypeError(f"notification switch for {category.id} must be a bool")
    return override
