from __future__ import annotations

import re
from pathlib import Path

__all__ = ["is_repo_test_file", "is_test_path", "split_only"]

_TEST_DIR = re.compile(r"(^|/)tests?/")
_ONLY_FLAG = "--only"


# meiva:note
# Whether a repo-relative posix path sits inside any `tests/` directory.
def is_test_path(rel_path: str) -> bool:
    return _TEST_DIR.search(rel_path) is not None


# meiva:note
# `is_test_path` for an absolute path; a path outside the repo is never a test.
def is_repo_test_file(path: Path, repo_root: Path) -> bool:
    try:
        return is_test_path(path.relative_to(repo_root).as_posix())
    except ValueError:
        return False


# meiva:note
# Split `--only FILE...` (consuming the rest of argv) off a lint's arguments.
# Returns the arguments before the flag and the changed files, or None when the
# flag is absent; an empty list means nothing changed.
def split_only(argv: list[str]) -> tuple[list[str], list[str] | None]:
    if _ONLY_FLAG not in argv:
        return argv, None
    at = argv.index(_ONLY_FLAG)
    return argv[:at], argv[at + 1 :]
