from __future__ import annotations

from typing import Any, cast

import pytest

from base.events.notifications.catalog import (
    CORE_CATEGORIES,
    Importance,
    NotificationCategory,
    category_from_manifest,
    merge_catalog,
)


def _category(
    *,
    id: str = "plugin.event",
    label: str = "Plugin event",
    importance: Importance = "normal",
    default_enabled: bool = True,
    dedupe_window_seconds: int = 0,
) -> NotificationCategory:
    return NotificationCategory(
        id=id,
        label=label,
        description="A plugin declared its own category.",
        importance=importance,
        default_enabled=default_enabled,
        dedupe_window_seconds=dedupe_window_seconds,
    )


def test_core_ids_are_unique_and_namespaced() -> None:
    merged = merge_catalog((CORE_CATEGORIES,))
    assert set(merged) == {"core.needs_input"}
    assert all(category_id.startswith("core.") for category_id in merged)


# meiva:note
# The core catalog holds the one request the user has to answer. A turn that
# finished or a task that closed is a log line, so adding another core category
# is a product decision that starts here, not a convenience.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_the_core_catalog_is_the_request_that_needs_an_answer() -> None:
    assert [category.id for category in CORE_CATEGORIES] == ["core.needs_input"]


# meiva:note
# The default configuration has exactly one enabled category. A core category that
# ships enabled — a new event the user did not ask to be interrupted for — fails
# here. Enabling one is the user's switch; a plugin category cannot ship enabled at
# all (its manifest validator rejects `defaultEnabled: true`).
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def test_only_the_request_that_needs_a_reply_ships_enabled() -> None:
    enabled = {category.id for category in CORE_CATEGORIES if category.default_enabled}
    assert enabled == {"core.needs_input"}


def test_category_from_manifest_maps_every_declared_field() -> None:
    entry: dict[str, Any] = {
        "id": "plugin.event",
        "label": "Plugin event",
        "description": "A plugin asked the user a question.",
        "importance": "high",
        "defaultEnabled": False,
        "dedupeWindowSeconds": 42,
    }

    category = category_from_manifest(entry, plugin="plugin")

    assert category == NotificationCategory(
        id="plugin.event",
        label="Plugin event",
        description="A plugin asked the user a question.",
        importance="high",
        default_enabled=False,
        dedupe_window_seconds=42,
    )


def test_category_from_manifest_rejects_a_foreign_namespace() -> None:
    entry: dict[str, Any] = {
        "id": "other.event",
        "label": "Other event",
        "description": "",
        "importance": "normal",
        "defaultEnabled": False,
        "dedupeWindowSeconds": 0,
    }

    with pytest.raises(ValueError, match="outside its own namespace"):
        category_from_manifest(entry, plugin="plugin")


def test_duplicate_id_across_owners_is_rejected() -> None:
    with pytest.raises(ValueError, match="declared twice"):
        merge_catalog((CORE_CATEGORIES, (CORE_CATEGORIES[0],)))


def test_id_must_be_namespaced() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        _category(id="turn_finished")


def test_unknown_importance_is_rejected() -> None:
    with pytest.raises(ValueError, match="importance"):
        _category(importance=cast(Importance, "critical"))


def test_negative_dedupe_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="dedupe window"):
        _category(dedupe_window_seconds=-1)


def test_empty_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="label"):
        _category(label="  ")
