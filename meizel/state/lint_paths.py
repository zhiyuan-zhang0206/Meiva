# meiva:note
# What both config-read rules share about the paths they scan: how a path is labelled in a finding,
# and which explicit targets are Python files the scan reads. The shells resolve, walk and read;
# nothing here touches the filesystem.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

__all__ = ["label", "python_paths"]


# meiva:note
# The explicit targets a scan reads: the ones that are Python files. A directory target is not
# expanded here — naming a directory means the default scan, which the caller reaches without one.
def python_paths(root: Path, resolved: Iterable[str]) -> list[Path]:
    return [root / entry for entry in resolved if entry.endswith(".py")]


# meiva:note
# How a path is labelled in a finding: repository-relative when it is inside the repository, its
# absolute posix form when it is not.
def label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
