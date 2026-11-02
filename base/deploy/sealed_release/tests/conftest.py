# meiva:note
# A sealed tree refuses pytest's own removal of `tmp_path` (no write bits, and on macOS the
# immutable flag), so a test that seals registers each path with the host that sealed it and the
# fixture unseals them at teardown.

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from base.deploy.sealed_release.sealed_tree import unseal
from base.host.system.hostport import Host


@pytest.fixture
def sealed_paths() -> Iterator[list[tuple[Path, Host]]]:
    """Append `(path, host)` for every path a test seals; each is unsealed at teardown."""
    paths: list[tuple[Path, Host]] = []
    yield paths
    for path, host in reversed(paths):
        if path.exists():
            unseal(path, host=host)
