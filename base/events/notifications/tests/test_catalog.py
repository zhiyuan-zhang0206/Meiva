from __future__ import annotations

import pytest

from base.events.notifications.catalog import (
    CORE_CATEGORIES,
    QUESTION_CATEGORY,
    UNREAD_CATEGORY,
    category,
)


def test_the_catalog_has_exactly_the_two_kinds_of_push() -> None:
    assert {item.id for item in CORE_CATEGORIES} == {QUESTION_CATEGORY, UNREAD_CATEGORY}


def test_an_unknown_category_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown notification category"):
        category("plugin.unknown")
