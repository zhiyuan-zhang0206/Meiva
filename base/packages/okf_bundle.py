# meiva:note
# The shell of the OKF graph: walk a bundle directory and read its notes.
#
# `base.packages.docs.okf_graph` builds the graph from a mapping of path to text and does no I/O;
# this is the one place that turns a directory into that mapping.

from __future__ import annotations

import os
from pathlib import Path

from base.packages.docs.okf_graph import EXT

__all__ = ["read_bundle"]


# meiva:note
# Every `.meiva.okf.md` note under `bundle_dir`, as `{bundle-relative posix path: text}`.
#
# `.github/` is walked despite being hidden: its overview node lives inside it
# (`.github/.github.meiva.okf.md`). Every other hidden dir (`.git`, `.venv`, `.claude`,
# worktrees, ...) stays excluded. A note is read whole, whatever its size; one that cannot be
# read as UTF-8 raises instead of vanishing from the graph.
def read_bundle(bundle_dir: Path) -> dict[str, str]:
    notes: dict[str, str] = {}
    for root, dirs, names in os.walk(bundle_dir):
        dirs[:] = [d for d in dirs if (not d.startswith(".") or d == ".github") and d != ".git"]
        for name in names:
            if name.endswith(EXT):
                path = Path(root) / name
                notes[path.relative_to(bundle_dir).as_posix()] = path.read_text(encoding="utf-8")
    return notes
