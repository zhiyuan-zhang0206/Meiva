"""Consumer-local opt-in fixtures for this test directory."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.model_registry import check_model_updates


@pytest.fixture
def bare_tracker() -> Any:
    """The tracker script module with its provider-reply DoH cache emptied for the test."""
    check_model_updates.doh_cache.clear()
    return check_model_updates


@pytest.fixture(name="tracker")
def _tracker_with_empty_catalog(bare_tracker: Any) -> Any:
    """The tracker module for unit tests."""
    return bare_tracker
