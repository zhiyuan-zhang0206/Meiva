from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meizel.state.ambient_state import allowlist as ambient_allowlist

_LIST_NAMES = (
    "SINK_FACADES",
    "ALLOWED",
    "PURE_REPO_CALLEES",
    "SLICED_PACKAGES",
    "DB_HANDLE_PACKAGES",
    "ENDPOINT_PACKAGES",
    "BUS_PACKAGES",
)


@pytest.fixture(autouse=True)
def synthetic_ambient_allowlists() -> Iterator[None]:
    """Keep the real repository's ambient-state lists out of synthetic roots."""
    saved: dict[str, dict[Any, Any]] = {}
    for name in _LIST_NAMES:
        table: dict[Any, Any] = getattr(ambient_allowlist, name)
        saved[name] = dict(table)
        table.clear()
    try:
        yield
    finally:
        for name, orig in saved.items():
            table = getattr(ambient_allowlist, name)
            table.clear()
            table.update(orig)
