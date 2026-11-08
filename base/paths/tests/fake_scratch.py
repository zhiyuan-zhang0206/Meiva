# meiva:note
# A `ScratchSpace` a test controls: every directory it mints lives under a root the test names.

from __future__ import annotations

import shutil
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


# meiva:note
# Mints directories under `root` and records each one it minted. A directory is removed when its
# context exits, as `TempFileScratch`'s is, and `minted` keeps the paths so a test can assert what
# was asked for, and find what a failure left behind.
class FakeScratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._minted: list[Path] = []

    @property
    def minted(self) -> tuple[Path, ...]:
        return tuple(self._minted)

    @contextmanager
    def make(self, *, prefix: str = "", parent: Path | None = None) -> Generator[Path]:
        home = parent if parent is not None else self.root
        directory = home / f"{prefix}{len(self._minted) + 1}"
        directory.mkdir(parents=True)
        self._minted.append(directory)
        try:
            yield directory
        finally:
            shutil.rmtree(directory, ignore_errors=True)
