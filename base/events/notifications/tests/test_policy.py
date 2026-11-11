from __future__ import annotations

import pytest

from base.events.notifications.catalog import (
    CORE_CATEGORIES,
    QUESTION_CATEGORY,
    UNREAD_CATEGORY,
    category,
)
from base.events.notifications.policy import enabled_for


def test_the_catalog_has_exactly_the_two_kinds_of_push_and_both_ship_on() -> None:
    assert {item.id for item in CORE_CATEGORIES} == {QUESTION_CATEGORY, UNREAD_CATEGORY}
    assert all(item.default_enabled for item in CORE_CATEGORIES)


def test_an_unknown_category_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown notification category"):
        category("plugin.unknown")


def test_a_category_follows_its_declared_default_without_an_override() -> None:
    assert enabled_for(category(UNREAD_CATEGORY), {})


def test_an_override_switches_one_category_only() -> None:
    switches = {UNREAD_CATEGORY: False}
    assert not enabled_for(category(UNREAD_CATEGORY), switches)
    assert enabled_for(category(QUESTION_CATEGORY), switches)
