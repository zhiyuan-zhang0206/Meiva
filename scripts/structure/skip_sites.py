# meiva:note
# Skip sites in the test tree, frozen as one number that may only shrink.
#
# A skip is an assertion that stopped running: `@pytest.mark.skipif(...)`,
# `@pytest.mark.skip(...)`, `pytest.skip(...)` and `pytest.importorskip(...)`. The number
# is a budget, not a whitelist — the check counts sites and never matches them against a
# vocabulary, so a skip whose predicate this tree has never used (`os.environ.get("X")`,
# a marker, a helper that computes the condition) fails the moment it is added. One
# number for the whole tree, because a per-file budget invites trading: delete someone
# else's skip, add your own, and both budgets stay satisfied.
#
# The record under the number is what makes a failure name a site. A count alone can only
# say "one more than before"; the entries are the same fact per site, so a run that adds
# one prints its file, line and predicate instead of asking a reader to diff the whole
# tree. `tests/ci/test_collection_roots.py` owns which files are test modules; this
# module only counts what they skip. Its test (`scripts/structure/tests/test_skip_sites.py`)
# is the gate CI runs, in the pytest shards like every other tree-wide rule.
#
# After removing a skip: `.venv/bin/python -m scripts.structure.skip_sites --write-baseline`
# rewrites the record and prints the number to put here.

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import TypeGuard

_REPO_ROOT = Path(__file__).resolve().parents[2]

# meiva:note
# Every site the number counts: `<file>` -> the `what` strings found there, written by
# `--write-baseline`, never by hand. The sum of the entries IS `TEST_SKIP_SITES_ALLOWED`,
# and the difference against a run is what names an added or removed site.
BASELINE = "scripts/structure/skip_sites.json"

# meiva:note
# Allowed skip sites in the test tree: only shrink, never grow. Raising it is a decision
# someone has to defend in review, which is why it is one line in this file.
TEST_SKIP_SITES_ALLOWED = 58


# meiva:note
# `a.b.c` for an attribute chain, so a call can be named by what it calls.
def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _argument(call: ast.Call) -> str:
    for node in [*call.args, *[keyword.value for keyword in call.keywords]]:
        return ast.unparse(node)
    return ""


# meiva:note
# One `what` per skip site in `src`: `skipif(<predicate>)`, `skip(<reason>)`,
# `importorskip(<module>)`. Counting calls, not markers, is what keeps a decorated test
# counted once: the decorator's attribute and its call are the same site.
def site_strings(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func) or ""
        if name in ("pytest.skip", "pytest.importorskip"):
            out.append((node.lineno, f"{name.rsplit('.', 1)[1]}({_argument(node)})"))
            continue
        mark = name.rsplit(".mark.", 1)[-1] if ".mark." in name else ""
        if mark in ("skipif", "skip"):
            out.append((node.lineno, f"{mark}({_argument(node)})"))
    return sorted(out)


# meiva:note
# A tracked module a bare `pytest` collects: `test_*.py` inside a `tests/` directory.
def _is_test_module(rel: str) -> bool:
    *directories, name = rel.split("/")
    return name.startswith("test_") and name.endswith(".py") and "tests" in directories


# meiva:note
# Tracked, not on-disk, so a stray scratch file in a checkout cannot move the number.
def tracked_test_modules(root: Path = _REPO_ROOT) -> list[str]:
    listing = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [rel for rel in listing.stdout.split("\0") if _is_test_module(rel)]


# meiva:note
# The skip sites of `modules`, as `<file>` -> [(line, what)] in file order.
def sites_by_file(modules: list[str], root: Path = _REPO_ROOT) -> dict[str, list[tuple[int, str]]]:
    found: dict[str, list[tuple[int, str]]] = {}
    for rel in modules:
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            sites = site_strings(source)
        except SyntaxError:
            continue
        if sites:
            found[rel] = sites
    return found


def _counted(found: dict[str, list[tuple[int, str]]]) -> dict[str, Counter[str]]:
    return {path: Counter(what for _, what in sites) for path, sites in found.items()}


# meiva:note
# None when the run matches the record, else the report a reader acts on: the count
# against the number, the sites that moved with their file:line and predicate, and what
# to do about it.
def judge(
    found: dict[str, list[tuple[int, str]]],
    *,
    allowed: int,
    baseline: dict[str, tuple[str, ...]],
) -> str | None:
    total = sum(len(sites) for sites in found.values())
    added, removed = _difference(_counted(found), baseline)
    if not added and not removed and total == allowed:
        return None
    return _report(
        found, total=total, allowed=allowed, baseline=baseline, added=added, removed=removed
    )


def _difference(
    current: dict[str, Counter[str]], baseline: dict[str, tuple[str, ...]]
) -> tuple[dict[str, Counter[str]], dict[str, Counter[str]]]:
    recorded = {path: Counter(whats) for path, whats in baseline.items()}
    added = _moved(
        {path: counts - recorded.get(path, Counter()) for path, counts in current.items()}
    )
    removed = _moved(
        {path: counts - current.get(path, Counter()) for path, counts in recorded.items()}
    )
    return added, removed


def _moved(counts: dict[str, Counter[str]]) -> dict[str, Counter[str]]:
    return {path: value for path, value in counts.items() if value}


def _report(
    found: dict[str, list[tuple[int, str]]],
    *,
    total: int,
    allowed: int,
    baseline: dict[str, tuple[str, ...]],
    added: dict[str, Counter[str]],
    removed: dict[str, Counter[str]],
) -> str:
    moved = bool(added or removed)
    lines = [_headline(total, allowed, added=bool(added), removed=bool(removed))]
    if not moved:
        lines.append(_unmoved(total, baseline))
    for path, counts in sorted(added.items()):
        lines.extend(_added_lines(found.get(path, []), path, counts))
    for path, counts in sorted(removed.items()):
        lines.extend(f"  -{count} {path}  {what}" for what, count in sorted(counts.items()))
    lines.append(
        "Allowed skip sites only shrink: delete the skip, or - when a skip was genuinely "
        "removed in this change - rewrite the record with "
        "`.venv/bin/python -m scripts.structure.skip_sites --write-baseline`."
    )
    return "\n".join(lines)


def _unmoved(total: int, baseline: dict[str, tuple[str, ...]]) -> str:
    recorded = sum(len(whats) for whats in baseline.values())
    return (
        f"  the record holds {recorded} site(s) and the tree has {total}: the number and the "
        "record are the same fact, so change them only by changing the tree."
    )


def _headline(total: int, allowed: int, *, added: bool, removed: bool) -> str:
    if total > allowed and (added or removed):
        return (
            f"tests: skip sites {total} > allowed {allowed} - this run skips where the tree "
            "did not, at:"
        )
    if total < allowed and (added or removed):
        return (
            f"tests: skip sites {total} < allowed {allowed} - the tree skips less than the "
            "number says; record the reduction at:"
        )
    if total > allowed or total < allowed:
        return f"tests: skip sites {total} > allowed {allowed} - but the tree did not move."
    if added and removed:
        return (
            f"tests: skip sites {total} = allowed {allowed}, but not the recorded ones - a "
            "skip was added and another removed, at:"
        )
    if added:
        return (
            f"tests: skip sites {total} = allowed {allowed}, but the record is missing a site "
            "- the tree skips one it never recorded, at:"
        )
    return (
        f"tests: skip sites {total} = allowed {allowed}, but the record lists a site the tree "
        "no longer has, at:"
    )


def _added_lines(sites: list[tuple[int, str]], path: str, counts: Counter[str]) -> list[str]:
    out: list[str] = []
    for what, count in sorted(counts.items()):
        lines = [line for line, text in sites if text == what][:count]
        out.extend(f"  +{path}:{line}  {what}" for line in lines)
    return out


# meiva:note
# The record as written to disk: `<file>` -> its `what` strings, sorted, one per line.
def record(found: dict[str, list[tuple[int, str]]]) -> dict[str, list[str]]:
    return {path: sorted(what for _, what in sites) for path, sites in sorted(found.items())}


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# The recorded sites, or an empty record when the file is missing (a fresh checkout reads
# as "nothing recorded yet" and the gate reports every site as added).
def load(root: Path = _REPO_ROOT) -> dict[str, tuple[str, ...]]:
    path = root / BASELINE
    if not path.is_file():
        return {}
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not _is_str_object_dict(raw):
        raise SystemExit(f"{BASELINE}: expected a JSON object of path -> [what]")
    result: dict[str, tuple[str, ...]] = {}
    for key, value in raw.items():
        if not _is_object_list(value):
            raise SystemExit(f"{BASELINE}: expected a JSON object of path -> [what]")
        result[key] = tuple(str(item) for item in value)
    return result


# meiva:note
# Write the record of the tree this runs in: the only supported way to lower the number.
def write_baseline(found: dict[str, list[tuple[int, str]]], root: Path = _REPO_ROOT) -> int:
    path = root / BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    written = record(found)
    path.write_text(json.dumps(written, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(whats) for whats in written.values())
    sys.stdout.write(f"{BASELINE}: {total} site(s) in {len(written)} files\n")
    sys.stdout.write(f"set TEST_SKIP_SITES_ALLOWED = {total}\n")
    return total


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    found = sites_by_file(tracked_test_modules())
    if "--write-baseline" in arguments:
        write_baseline(found)
        return 0
    report = judge(found, allowed=TEST_SKIP_SITES_ALLOWED, baseline=load())
    if report is None:
        return 0
    sys.stderr.write(report + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
