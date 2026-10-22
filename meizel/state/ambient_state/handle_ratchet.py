"""Reject library-built database and event-bus handles without exemptions.

Library components receive handles from their composition root. Every detected
shim dial or self-built handle fails; there is no stored allowance or write mode.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from meizel import lint_common  # noqa: E402 - standalone script
from meizel.state import ambient_state  # noqa: E402 - standalone script
from meizel.state.ambient_state import busrule, dbhandle  # noqa: E402 - standalone script

SHIM = "shim"
SELF_BUILT = "self-built"
BUS_BUILT = "bus-built"
Sites = dict[tuple[str, str], list[str]]


def package_of(rel: str) -> str:
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def kind_of(name: str) -> str:
    return SELF_BUILT if name.endswith(".from_settings") else SHIM


# meiva:note
# `(package, kind) -> ["path:line", ...]` for every ungoverned production module.
def scan(
    repo_root: Path,
    *,
    tracked_files_fn: Callable[[Path], list[str]] = lint_common.tracked_files,
) -> Sites:
    found: Sites = {}
    for rel in tracked_files_fn(repo_root):
        if not rel.endswith(".py") or not ambient_state.in_scope(rel):
            continue
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        library = [
            (kind_of(hit.name), hit)
            for hit in (dbhandle.dials(tree) if dbhandle.package_of(rel) is None else [])
        ] + [
            (BUS_BUILT, hit)
            for hit in (busrule.builds(tree) if busrule.package_of(rel) is None else [])
        ]
        for kind, hit in library:
            found.setdefault((package_of(rel), kind), []).append(f"{rel}:{hit.line}")
    return found


FIX = {
    SHIM: "take a database handle from the composition root instead",
    SELF_BUILT: "take a database handle from the composition root instead",
    BUS_BUILT: "take an event-bus handle from the composition root instead",
}


def errors(sites: Sites) -> list[str]:
    return [
        f"{where}: `{package}` constructs a {kind} handle — {FIX[kind]}"
        for (package, kind), locations in sorted(sites.items())
        for where in locations
    ]


def main(
    argv: list[str],
    *,
    repo_root: Path = _REPO_ROOT,
    tracked_files_fn: Callable[[Path], list[str]] = lint_common.tracked_files,
) -> int:
    if argv:
        print("error: library handle lint accepts no arguments", file=sys.stderr)
        return 1
    problems = errors(scan(repo_root, tracked_files_fn=tracked_files_fn))
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
