# meiva:note
# What the cleanup of one scheduled operation removes, as values in and values out.
#
# `staging.cleanup` lists what is in the private staging directory and executes the
# removals this module names. The rule: a staged artifact directory keeps only complete,
# renamed `.dump.enc` regular files (plaintext and half-written pipeline files never
# survive), and a plaintext scratch directory goes entirely.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from services.backup.artifact.names import DUMP_NAME_RE

__all__ = ["Entry", "Removal", "artifact_removals", "scratch_removal"]


# meiva:note
# One directory entry as the shell saw it.
@dataclass(frozen=True)
class Entry:
    name: str
    is_file: bool
    is_dir: bool
    is_symlink: bool


# meiva:note
# One removal: the entry's name, and whether it is a directory tree (`rmtree`) or a
# single file or link (`unlink`).
@dataclass(frozen=True)
class Removal:
    name: str
    tree: bool


# meiva:note
# A renamed `.dump.enc` exists only after encryption completed.
def _complete(entry: Entry) -> bool:
    return (
        entry.is_file
        and not entry.is_symlink
        and entry.name.endswith(".dump.enc")
        and DUMP_NAME_RE.fullmatch(entry.name) is not None
    )


# meiva:note
# The removals that leave only complete encrypted artifacts: real directories go as
# trees, everything else that is not a complete artifact (links included) as files.
def artifact_removals(entries: Sequence[Entry]) -> list[Removal]:
    removals: list[Removal] = []
    for entry in entries:
        if entry.is_dir and not entry.is_symlink:
            removals.append(Removal(entry.name, tree=True))
        elif not _complete(entry):
            removals.append(Removal(entry.name, tree=False))
    return removals


# meiva:note
# The removal of a plaintext scratch directory, or None when there is none to remove (it
# is absent, a plain file, or a link that must not be followed).
def scratch_removal(entry: Entry) -> Removal | None:
    if not entry.is_dir or entry.is_symlink:
        return None
    return Removal(entry.name, tree=True)
