from __future__ import annotations

from typing import cast

import pytest

from base.events.notifications.catalog import (
    CORE_CATEGORIES,
    Importance,
    NotificationCategory,
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
    assert set(merged) == {"core.turn_finished", "core.needs_input"}
    assert all(category_id.startswith("core.") for category_id in merged)


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
