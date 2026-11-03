#!/usr/bin/env python3
# meiva:note
# Keep the impacted-closure pyright run inside the same scope as the full run.
#
# `[tool.pyright].include` and `exclude` decide what a full `basedpyright` run checks. Files
# passed on the command line are checked wherever they sit, so the pull-request step that hands
# `meizel impacted` output to basedpyright would check `schedules/` or `demos/` (not in
# `include`) as soon as the closure reached them, with the same rules as the included tree.
# This filter reads the two lists from pyproject.toml, keeps the paths the full run would check
# and reports the rest on stderr:
#
#     scripts/ci/pull_requests/pyright_scope.py impacted.txt > scoped.txt
#
# One repository-relative path per line in and out. `include` entries are directories (or files);
# `exclude` entries are pyright's globs: `**` spans directories, `*` stays inside a segment, and
# a pattern also excludes everything below a directory it matches.

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _config(pyproject_text: str) -> tuple[list[str], list[str]]:
    pyright = tomllib.loads(pyproject_text)["tool"]["pyright"]
    include: list[str] = [str(entry) for entry in pyright.get("include", [])]
    exclude: list[str] = [str(entry) for entry in pyright.get("exclude", [])]
    return include, exclude


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    for segment in pattern.strip("/").split("/"):
        if segment == "**":
            parts.append("(?:[^/]+/)*")
        else:
            parts.append(re.escape(segment).replace(r"\*", "[^/]*").replace(r"\?", "[^/]") + "/")
    body = "".join(parts).removesuffix("/")
    return re.compile(rf"(?:{body})(?:/.*)?")


# meiva:note
# Whether a full run with this `include` / `exclude` would check the repo-relative path.
def in_scope(path: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    included = any(
        path == root.strip("/") or path.startswith(root.strip("/") + "/") for root in include
    )
    return included and not any(_glob_regex(pattern).fullmatch(path) for pattern in exclude)


# meiva:note
# (kept, skipped): the paths inside and outside the scope pyproject.toml defines.
def filter_paths(paths: Iterable[str], pyproject_text: str) -> tuple[list[str], list[str]]:
    include, exclude = _config(pyproject_text)
    kept: list[str] = []
    skipped: list[str] = []
    for path in paths:
        (kept if in_scope(path, include, exclude) else skipped).append(path)
    return kept, skipped


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Filter impacted paths to pyright's include.")
    parser.add_argument("paths_file", type=Path, help="one repository-relative path per line")
    parser.add_argument("--pyproject", type=Path, default=_REPOSITORY_ROOT / "pyproject.toml")
    args = parser.parse_args(argv)
    paths = [line.strip() for line in args.paths_file.read_text().splitlines() if line.strip()]
    kept, skipped = filter_paths(paths, args.pyproject.read_text(encoding="utf-8"))
    for path in kept:
        print(path)
    if skipped:
        print(
            f"{len(skipped)} impacted module(s) outside pyright include skipped: "
            f"{', '.join(skipped)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
