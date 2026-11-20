# meiva:note
# The input closure of every collectable test, read off the selector's own rules. `meizel test-cache
# plan --closures-file` keys a test's recorded result by the content of its closure. The closure here
# is the inverse of `scripts/ci/test_selector.py`: a file is in a test's closure exactly when changing
# that file alone selects the test, so a cached result is never reused where the selector would run the
# test. A test the selector cannot bound (an input the analysis cannot see, or a read of the whole
# tree) is keyed by every file of the tree: its entry names the reason and lists no files.
# meiva:link docs/conventions/engineering/test-selection.md
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.options import options
from scripts.ci import test_selector
from scripts.ci.shard_counts import load_durations
from scripts.ci.test_impact import Impact, build_impact, unbounded_tests
from scripts.ci.test_selector import Checkout, PathClass

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# meiva:note
# The classes whose change the selector answers with the whole suite.
_WHOLE_SUITE = frozenset({PathClass.GLOBAL, PathClass.UNMAPPED})

_Group = tuple[str, str, str | None]


# meiva:note
# The document marker the selector pins readers by (see `test_selector._doc_readers`).
def _needle(path: str) -> str | None:
    name = Path(path).name
    return "SKILL.md" if name == "SKILL.md" else ".okf.md" if name.endswith(".okf.md") else None


# meiva:note
# What a class rule's answer depends on besides the class: the referenced root for a tree-scan-only
# path, the directory for the rest, and the file itself for a test file.
def _group(path: str, kind: PathClass) -> _Group:
    if kind is PathClass.TEST:
        return (kind.value, path, None)
    if kind is PathClass.TREE_SCAN_ONLY:
        return (kind.value, path.split("/", maxsplit=1)[0], _needle(path))
    return (kind.value, path.rpartition("/")[0], _needle(path))


# meiva:note
# The tests the selector picks when `path` is the only changed file. With no runtime consumers the
# rule's answer is its class part alone, which depends only on the group; this very path's consumers
# are added after the memo.
def _tests_of(
    path: str,
    kind: PathClass,
    checkout: Checkout,
    impact: Impact,
    memo: dict[_Group, frozenset[str]],
) -> frozenset[str]:
    if kind in _WHOLE_SUITE:
        return checkout.collectable
    if kind is PathClass.MANIFEST:
        return frozenset(test_selector.path_tests(path, kind, checkout, impact))
    key = _group(path, kind)
    if key not in memo:
        memo[key] = frozenset(test_selector.path_tests(path, kind, checkout, Impact({}, ())))
    return memo[key] | impact.runtime_tests(path)


# meiva:note
# Each collectable test's closure over `paths`: `{"files": [...]}`, or `{"unbounded": reason}` when
# the selector cannot bound its inputs. The selector adds a test with an input it cannot see to every
# selection; a test whose unseen inputs are data reads only joins a selection that changes a data file
# (`is_data_change`), so it is bounded by the data files rather than by the whole tree.
def closures(repo_root: Path, paths: Iterable[str]) -> dict[str, dict[str, object]]:
    repo_root = repo_root.resolve()
    checkout = test_selector.load_checkout(repo_root)
    impact = build_impact(repo_root, checkout.collectable)
    whole = unbounded_tests(impact, data_changed=False)
    data_readers = unbounded_tests(impact, data_changed=True) - whole
    unbounded = dict.fromkeys(sorted(whole), "unbounded-input")
    for test in sorted(test_selector.tree_scan_tests(repo_root)):
        _ = unbounded.setdefault(test, "tree-scan")
    bounded: dict[str, set[str]] = {test: set() for test in checkout.collectable}
    memo: dict[_Group, frozenset[str]] = {}
    for path in sorted(set(paths)):
        kind = test_selector.classify_path(path, checkout)
        if kind is PathClass.DELETED:
            continue
        selected = _tests_of(path, kind, checkout, impact, memo)
        if test_selector.is_data_change(path, kind):
            selected = selected | data_readers
        for test in selected & checkout.collectable:
            bounded[test].add(path)
    return {
        test: {"unbounded": unbounded[test]} if test in unbounded else {"files": sorted(files)}
        for test, files in sorted(bounded.items())
    }


# meiva:note
# The tracked and the untracked, non-ignored files of a git checkout.
def _checkout_paths(repo_root: Path) -> list[str]:
    listing = subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(repo_root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        capture_output=True,
        check=True,
    )
    return sorted({name.decode() for name in listing.stdout.split(b"\0") if name})


# meiva:note
# The seconds each test file takes, summed from the node timings in `.test_durations`.
def file_durations(repo_root: Path) -> dict[str, float]:
    totals: dict[str, float] = {}
    for node_id, seconds in load_durations(repo_root / ".test_durations").items():
        path = node_id.partition("::")[0]
        totals[path] = totals.get(path, 0.0) + seconds
    return totals


@dataclass(frozen=True)
class Options:
    what: str
    repo_root: Path
    out: Path


# meiva:note
# Write the closures, or the file durations, of the checkout at `--repo-root` to `--out`.
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Per-test input closures for the test result cache."
    )
    _ = parser.add_argument("what", choices=("closures", "durations"))
    _ = parser.add_argument("--repo-root", type=Path, default=_REPOSITORY_ROOT)
    _ = parser.add_argument("--out", type=Path, required=True)
    args = options(parser.parse_args(argv), Options)
    if args.what == "durations":
        result: dict[str, object] = dict(file_durations(args.repo_root))
    else:
        result = dict(closures(args.repo_root, _checkout_paths(args.repo_root)))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    _ = args.out.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    if args.what == "closures":
        bounded = sum(
            1 for entry in result.values() if isinstance(entry, dict) and "files" in entry
        )
        print(
            f"test-cache closures: {len(result)} tests, {bounded} bounded, "
            f"{len(result) - bounded} whole-tree"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
