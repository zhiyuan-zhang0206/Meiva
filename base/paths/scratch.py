# meiva:note
# The process's temporary space: the one place a scratch directory is minted.
#
# A module that needs a directory for one call takes a `ScratchSpace` and mints it there; it never
# imports `tempfile`. A test hands it a `FakeScratch` over `tmp_path`, an entry hands it
# `SYSTEM_SCRATCH`, and everything between passes down what it was given.
#
# It sits beside `private_dir.py`, which states which directories on this machine are the process's
# own; this module states where the ones that are thrown away are minted. It is the host boundary
# that owns that space, so it is a `[roots]` composition point in `meizel.toml`: the environment's
# answer for where temporary space lives (`TMPDIR` and its siblings) and the OS's random name for
# the directory under it are read here and nowhere else. A call that names its own parent passes the
# caller's directory down and reads neither.

from __future__ import annotations

import tempfile
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol


# meiva:note
# A place to mint a directory nothing hands in, and the root it mints under.
class ScratchSpace(Protocol):
    # meiva:note
    # The temporary root of the process: where a directory no caller names belongs.
    @property
    def root(self) -> Path: ...

    # meiva:note
    # A fresh directory, removed when the context exits; `parent` names the directory it is minted
    # in (a caller that holds one passes it and the space's own root is never consulted), and
    # `prefix` keeps a leftover recognisable.
    def make(
        self, *, prefix: str = "", parent: Path | None = None
    ) -> AbstractContextManager[Path]: ...


# meiva:note
# The process's own temporary space, as the host's `tempfile` reports it.
class TempFileScratch:
    # meiva:note
    # `gettempdir` is the environment's answer, cached by `tempfile` for the life of the process.
    @property
    def root(self) -> Path:
        return Path(tempfile.gettempdir())

    # meiva:note
    # Two calls, not one with `dir=parent`: a `dir` argument spares the whole call from the ambient
    # scan, so `TemporaryDirectory(prefix, dir=parent)` would hide the draw a `parent=None` really
    # makes. Written apart, the ledger sees the call that reads the environment and draws the name,
    # and the one that only takes the caller's directory.
    @contextmanager
    def make(self, *, prefix: str = "", parent: Path | None = None) -> Generator[Path]:
        space = (
            tempfile.TemporaryDirectory(prefix=prefix)
            if parent is None
            else tempfile.TemporaryDirectory(prefix=prefix, dir=parent)
        )
        with space as name:
            yield Path(name)


# meiva:note
# The one instance a process entry hands down. It holds no state: every directory is minted by the
# call that needs it, so two entries in one process may share this value.
SYSTEM_SCRATCH: ScratchSpace = TempFileScratch()
