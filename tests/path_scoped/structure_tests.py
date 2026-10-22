"""Shared isolation for the structure-gate tests (registered by `tests/fixtures/path_scopes.py`)."""

from __future__ import annotations

from meizel.tests.synthetic import (
    synthetic_ambient_allowlists as synthetic_ambient_allowlists,
)
from meizel.tests.synthetic import (
    synthetic_decision_allowlists as synthetic_decision_allowlists,
)

__all__ = [
    "synthetic_ambient_allowlists",
    "synthetic_decision_allowlists",
]
