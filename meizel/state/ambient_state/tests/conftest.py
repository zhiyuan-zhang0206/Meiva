from __future__ import annotations

import pytest

from meizel.state.ambient_state import allowlist as ambient_allowlist


@pytest.fixture(autouse=True)
def synthetic_ambient_allowlists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real repository's ambient-state lists out of synthetic roots."""
    for name in (
        "SINK_FACADES",
        "ALLOWED",
        "PURE_REPO_CALLEES",
        "SLICED_PACKAGES",
        "DB_HANDLE_PACKAGES",
        "ENDPOINT_PACKAGES",
        "BUS_PACKAGES",
    ):
        monkeypatch.setattr(ambient_allowlist, name, {})
