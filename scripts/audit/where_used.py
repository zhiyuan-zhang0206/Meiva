# meiva:note
# Find every reference to a symbol, module or file before you change or move it.
#
# Run: `.venv/bin/python scripts/audit/where_used.py TARGET [TARGET ...] [--limit N] [--json]`
#
# ## Why
#
# The slow part of most changes is not finding the first file to edit, it is
# enumerating the rest of the edit set: importers, tests, string targets, docs and
# registries. This command answers that in one step, in seconds, instead of a chain
# of greps. `module_moves.py` is the complementary check for AFTER a move (no
# reference to the old path may remain); this one is the question BEFORE.
#
# ## Targets
#
# - a symbol: `pkg.mod:name`, `pkg.mod.name` or `pkg/mod.py::name` (must be bound at
#   the top level of that module; the name is checked, so a typo fails fast);
# - a module or package: `pkg.mod`, `pkg/mod.py` or `pkg` (a package covers every
#   module below it);
# - any other tracked path: a file (`docs/conventions/runbook.md`) or a directory.
#
# Several targets may be given; the repository is read once.
#
# ## Groups
#
# Every hit lands in exactly one group, shown as `path:line  [kind]  context`, one
# entry per file, every file of the group unless `--limit N` caps the files listed per
# group (a capped group says how many it left out, and the header always carries the full
# count):
#
# - `IMPORTS`: import statements of production code (absolute, relative, deferred in
#   a function, `from x import y`, wildcard) and, for a symbol, attribute use through
#   an imported module. A symbol that a package `__init__` imports at its top level is
#   also importable from that package: the package is named ("importable through") and
#   its importers are included.
# - `STRINGS`: dotted or path strings that name the target: patch targets
#   (`monkeypatch.setattr("a.b.c", ...)`, `patch("a.b.c")` and, for a symbol, the object
#   form `monkeypatch.setattr(mod, "name", ...)`), dynamic imports, `python -m a.b` entry
#   points, registries and configuration. A patch in a test is listed under `TESTS`.
# - `TESTS`: test files that reference it, plus tests named for the module that do not
#   (`test_<module>.py`). A test beside the code (`<pkg>/tests/`) is told apart from
#   the top-level `tests/`.
# - `DOCS`: markdown (links are resolved relative to the file), docstrings, comments.
#   A symbol also matches inside inline code in markdown (`` `name` ``); an ordinary
#   lowercase word (`run`, `scan`) only in markdown that names its module or sits beside
#   it. A file whose name is unique in the repository matches by that bare name.
# - `STRUCTURE`: the structure baseline shards, `pyproject.toml`, hooks, workflows and
#   the lint registries under `scripts/{structure,lint,content_lint}`.
# - `FROZEN HISTORY`: `docs/decisions/`, `docs/postmortems/` and `CHANGELOG.md`.
#   Historical references, never rewritten.
#
# References from inside the target (its own file, or a package's own modules) are
# counted and hidden: they move with it.
#
# ## Limits
#
# Static references only. Names built at runtime (f-strings, `getattr`, joined path
# pieces) and a symbol imported under another local name are invisible; a symbol is
# matched by its bare name only inside markdown inline code, never in code comments;
# members of a class are not tracked, name the class. Treat the output as the starting
# edit set, then verify with the changed code's own tests. Untracked files that are not
# ignored are scanned; `.test_durations` is not (it is regenerated). Exit status is 0
# whenever the target resolves; 2 when it does not.
# The reference finding itself lives in `analysis/references.py`; grouping and rendering are in
# `analysis/report.py`.
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from meizel import lint_common  # noqa: E402
from scripts.audit.analysis import (  # noqa: E402
    Repo,
    Report,
    as_json,
    index_repo,
    render,
    resolve,
    scan,
)

__all__ = ["main"]

_DESCRIPTION = (
    "Find every reference to a symbol, module or file before you change or move it: "
    "importers, tests, string targets, docs and registries. TARGET is a symbol (pkg.mod:name), "
    "a module or a path; the groups and what each one means are in the note at the top of this file."
)
# meiva:note
# `.test_durations` is regenerated, so it says nothing about where a name is used.
_GENERATED = frozenset({".test_durations"})


def load_repo(root: Path) -> Repo:
    # meiva:note
    # Read every tracked (and unignored untracked) text file once.
    texts: dict[str, str] = {}
    for name in lint_common.tracked_files(root, include_untracked=True):
        path = root / name
        if name in _GENERATED or path.is_symlink():
            continue
        text = lint_common.read_utf8_text(path)
        if text is not None:
            texts[name] = text
    return index_repo(texts)


def _relative(raw: str, repo_root: Path) -> str:
    # meiva:note
    # `/abs/path/in/this/checkout.py:name` -> `path/in/this/checkout.py:name`.
    spec, sep, rest = raw.partition(":")
    path = Path(spec)
    if path.is_absolute() and path.resolve().is_relative_to(repo_root):
        spec = path.resolve().relative_to(repo_root).as_posix()
    return spec + sep + rest


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    # meiva:note
    # Report references in an explicit checkout, defaulting to this checkout.
    #
    #     Absolute targets inside that checkout are normalized to checkout-relative
    #     paths. Command-line parsing, output groups and exit codes are unchanged.
    parser = argparse.ArgumentParser(
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("targets", nargs="+", metavar="TARGET", help="symbol, module or path")
    parser.add_argument(
        "--limit", type=int, default=None, help="list at most this many files per group"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="complete, for scripts")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    root = _REPO if repo_root is None else repo_root.resolve()
    repo = load_repo(root)
    reports: list[Report] = []
    for raw in args.targets:
        try:
            reports.append(scan(repo, resolve(_relative(raw, root), repo)))
        except ValueError as error:
            parser.error(str(error))
    if args.as_json:
        print(json.dumps([as_json(report) for report in reports], indent=2))
    else:
        print("\n\n".join(render(report, args.limit) for report in reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
