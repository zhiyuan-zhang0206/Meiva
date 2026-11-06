# meiva:note
# A file named like a test must sit where pytest looks, or not be a test at all.

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import cast

REPO = Path(__file__).resolve().parents[2]


# meiva:note
# pytest only walks `testpaths` (pyproject.toml). A file that defines tests but sits outside every
# one of those roots is never imported, never run, and never reported: pytest has nothing to say
# about a file it does not visit. The repository already names two such files on purpose
# (`base/db/test_db_guard.py`, `scripts/ci/test_selector.py` are production modules), so the
# dangerous half is narrow and worth pinning: a real test in that position (task #6361).
def _testpath_roots() -> list[str]:
    raw = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    tool = cast("dict[str, object]", raw["tool"])
    ini = cast("dict[str, object]", cast("dict[str, object]", tool["pytest"])["ini_options"])
    return [str(root) for root in cast("list[object]", ini["testpaths"])]


def _root_pattern(root: str) -> re.Pattern[str]:
    parts = [
        "(?:[^/]+/)*" if segment == "**" else re.escape(segment) + "/"
        for segment in root.split("/")
    ]
    return re.compile("^" + "".join(parts))


def _inside_a_testpath(path: str, roots: list[str]) -> bool:
    return any(_root_pattern(root).match(path) for root in roots)


_SKIP_DIRS = frozenset({".git", ".venv", ".cache", ".worktrees", "__pycache__", "node_modules"})


def _test_named_files() -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            if name.startswith("test_") and name.endswith(".py"):
                found.append((Path(dirpath) / name).relative_to(REPO).as_posix())
    return found


def _defines_a_test(path: str) -> bool:
    text = (REPO / path).read_text(encoding="utf-8", errors="replace")
    return any(line.lstrip().startswith("def test_") for line in text.splitlines())


def test_no_test_file_sits_outside_the_collection_roots() -> None:
    roots = _testpath_roots()
    invisible = [
        path
        for path in _test_named_files()
        if not _inside_a_testpath(path, roots) and _defines_a_test(path)
    ]
    assert not invisible, (
        "these files define tests but sit outside every testpaths root, so no job will ever run "
        "them: " + ", ".join(invisible)
    )
