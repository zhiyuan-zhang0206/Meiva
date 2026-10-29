from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meizel.checkout import roles_at
from meizel.roles import select_packages
from meizel.state.ambient_state import allowlist as ambient_allowlist

# meiva:note
# The directories the rule governs, read once from this repository: `measure` takes the scope as a
# value, so a test names the same declaration the gate names instead of inventing one.
SCOPE = select_packages(roles_at(Path(__file__).resolve().parents[4]), "application", "templates")

_LIST_NAMES = (
    "SINK_FACADES",
    "ALLOWED",
    "PURE_REPO_CALLEES",
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
