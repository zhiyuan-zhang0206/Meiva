# meiva:note
# Parse tool outputs into structured `diff-checks/v1` receipt fields and format the single-line
# receipt. Kept free of subprocesses and environment reads so scheduling and caching stay separate
# from verdict formatting.

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from meizel.checkout import python_reverse_closure_at
from meizel.graph.model import MeizelError
from scripts.hooks.diff_checks_pytest import spell_seconds

RECEIPT_VERSION = "diff-checks/v1"


def reverse_closure(root: Path, files: Sequence[str]) -> set[str] | None:
    if not root.is_dir():
        return None
    try:
        reached = python_reverse_closure_at(root, files)
    except MeizelError:
        return None
    return set(reached) if reached is not None else None


# meiva:note
# What one change looks like to a trigger: the repository root, the comparison base and the paths
# the change touches. A trigger reads this and decides whether its runs belong to this change.
@dataclass(frozen=True)
class ChangeSet:
    root: Path
    base: str
    files: tuple[str, ...]

    def python_files(self) -> tuple[str, ...]:
        return tuple(name for name in self.files if name.endswith(".py"))


# meiva:note
# One command a trigger asks for, named by the receipt field it fills. `judge` states how a reader
# decides red — an exit code, or a count against a threshold. The runner never reads output text
# for a verdict, so a check cannot turn green by printing the right word.
@dataclass(frozen=True)
class Run:
    label: str
    argv: tuple[str, ...]
    judge: str
    cwd: str = ""


# meiva:note
# What kinds of file a change touches. The receipt prints it (`scope=`), a trigger reads it, and
# the layers nothing ran take their reason from it: a documentation-only change still owes the
# cheap document checks, and a Python change must not pay for a debt comparison it cannot move.
_KIND_SUFFIXES = (("docs", ".md"), ("manifests", "meizel.toml"), ("python", ".py"))


def kinds(change: ChangeSet) -> frozenset[str]:
    found = {
        kind for kind, suffix in _KIND_SUFFIXES for name in change.files if name.endswith(suffix)
    }
    if any(name.startswith("ui/web/") for name in change.files):
        found.add("frontend")
    return frozenset(found)


def scope_of(change: ChangeSet) -> str:
    ordered = sorted(kinds(change))
    return "+".join(ordered) if ordered else "other"


# meiva:note
# The fields of a receipt, in the order a reader meets them, and the whole vocabulary a value may
# use: the count a layer measured, `n/a(<why>)` for a check this change cannot need, `skipped(<why>)`
# for one it needs and did not run, `unreadable(<why>)` for output the runner could not read, and
# `broken(<why>)` for a run that did not happen as asked -- a truncated run is not a red one.
# There is no bare `none` anywhere: "did not apply" and "ran, found nothing" must never read the
# same to the person merging on this line. No value carries a space -- the line is split on
# whitespace, so a reason joins its words with `-`.
FIELD_ORDER = (
    "scope",
    "pyright",
    "err_in_this_diff",
    "closure",
    "ruff",
    "selector",
    "tests",
    "passed",
    "failed",
    "errors",
    "blind",
    "fallback",
    "vitest-app",
    "next-build",
    "meizel",
    "debt",
    "refusal-loss",
    "docs-refs",
    "docs-okf",
)


# meiva:note
# The `N errors` count in a tool's summary, in both senses this runner reads it. pytest counts a
# test that could not even start as an error and never as a failure: `31151 passed, 538 errors` is
# a run in which 538 tests did not happen while `failed` stayed 0, so a receipt that parsed only
# the failure count printed `failed=0tests` for a run whose shell exited 1 (#8001, 2026-11-01: 2267
# files, 538 errors). pyright states its findings the same way (`9 errors, 0 warnings`). Each is
# its own field so a reader can tell "nothing failed" from "much never ran".
def error_count(output: str) -> int:
    found = re.search(r"(\d+) errors?\b", output)
    return int(found.group(1)) if found else 0


_ERROR_LINE = re.compile(r"^(?P<path>[^\s:][^:]*):\d+:\d+ - error:")


# meiva:note
# How many of the tree's errors sit in files this change touches. The total answers "is the
# tree green"; this answers "did I do it", and a receipt that only carried one of the two would
# let a reader mistake a local fact for a global one.
def _errors_in_diff(output: str, change: ChangeSet) -> int:
    touched = set(change.files)
    return sum(
        1
        for line in output.splitlines()
        if (found := _ERROR_LINE.match(line.strip()))
        and any(found.group("path").endswith(name) for name in touched)
    )


# meiva:note
# What the checker claimed: how many manifests and fact edges it read, and the exit code. A bare
# `ok` hides the difference between "covered the tree" and "found nothing to look at".
def _meizel_field(code: int, output: str, seconds: float) -> str:
    edges = re.search(r"(\d+) fact edges", output)
    manifests = re.search(r"(\d+) manifests", output)
    parts = [f"exit={code}"]
    if manifests is not None:
        parts.append(f"{manifests.group(1)}manifests")
    if edges is not None:
        parts.append(f"{edges.group(1)}edges")
    return f"{'ok' if code == 0 else 'failed'}({','.join(parts)})({spell_seconds(seconds)})"


def _debt_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
    header: list[str] = []
    rows: dict[str, list[str]] = {}
    for line in output.splitlines():
        cells = [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]
        if not cells:
            continue
        if cells[0] == "code":
            header = cells
            continue
        rows[cells[0]] = cells[1:]
    # meiva:note
    # Read the columns by position, never by the reference's own name: the first column after
    # `code` is whatever `--against` was given, which for a replay or a pull request base is a sha
    # or `<sha>~1`, and the column labelled `tree` is what this tree records. A change that edits a
    # rule or a declaration prints one more column before `tree`, which the label absorbs.
    if not header or header[0] != "code" or "tree" not in header:
        return f"unreadable(no-table)({spell_seconds(seconds)})"
    tree_column = header.index("tree") - 1
    ambient = rows.get("ambient-read")
    total = rows.get("total")
    if ambient is None or total is None or len(total) <= tree_column or len(ambient) <= tree_column:
        return f"unreadable(no-totals)({spell_seconds(seconds)})"
    return (
        f"{verdict}(ambient-read{ambient[0]}->{ambient[tree_column]},"
        f"total{total[0]}->{total[tree_column]})({spell_seconds(seconds)})"
    )


# meiva:note
# The document checks state how much they read; a clean run that names only itself would leave a
# reader unable to tell a scan of three files from a scan of the tree.
def _docs_refs_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
    found = re.search(r"(\d+) docs checked, (\d+) dangling reference", output)
    if found is None:
        return f"unreadable(no-summary)({spell_seconds(seconds)})"
    return f"{verdict}({found.group(1)}docs,{found.group(2)}dangling)({spell_seconds(seconds)})"


def _docs_okf_field(code: int, output: str, seconds: float) -> str:
    verdict = "ok" if code == 0 else f"failed({code})"
    found = re.search(r"(\d+) files checked, (\d+) error\(s\), (\d+) warning\(s\)", output)
    if found is None:
        return f"unreadable(no-summary)({spell_seconds(seconds)})"
    return (
        f"{verdict}({found.group(1)}files,{found.group(2)}errors,"
        f"{found.group(3)}warnings)({spell_seconds(seconds)})"
    )


def _refusal_field(output: str) -> str:
    found = re.search(r"refusal-loss: hits=(\d+) blocking=(\d+) unreplaced=(\d+)", output)
    if found is None:
        return "unreadable"
    return f"hits={found.group(1)},blocking={found.group(2)},unreplaced={found.group(3)}"


# meiva:note
# A clean tree says so in one field and stops: no closure scan, no attribution. A red tree is
# attributed to this change only when the errors sit in files that can see it; anything the scan
# cannot prove stays inside, because "the tool could not tell" and "not my code" must not look
# the same in a receipt a reader trusts.
def _pyright_fields(output: str, seconds: float, change: ChangeSet) -> dict[str, str]:
    errors = error_count(output)
    if errors == 0:
        return {
            "pyright": f"0err({spell_seconds(seconds)})",
            "err_in_this_diff": str(_errors_in_diff(output, change)),
        }
    reached = reverse_closure(change.root, change.files)
    if reached is None:
        return {
            "pyright": f"{errors}err({spell_seconds(seconds)})",
            "closure": "unknown",
            "_over_baseline": "1",
        }
    inside: list[str] = []
    outside: list[str] = []
    changed = set(change.files)
    root = str(change.root) + "/"
    for line in output.splitlines():
        found = _ERROR_LINE.match(line.strip())
        if found is None:
            continue
        name = found.group("path").removeprefix(root)
        if name in reached or name in changed:
            inside.append(name)
        else:
            outside.append(name)
    distinct = sorted(set(outside))
    shown = ",".join(distinct[:3])
    more = len(distinct) - 3
    tail = f"{shown},+{more}more" if more > 0 else shown or "none"
    return {
        "pyright": (
            f"{errors}err({len(inside)}in-closure,{len(outside)}elsewhere:{tail})"
            f"({spell_seconds(seconds)})"
        ),
        "err_in_this_diff": str(_errors_in_diff(output, change)),
        "_over_baseline": "1" if inside else "0",
    }


# meiva:note
# Vitest prints two counts: the files it read and the tests it answered. Only the second says how
# many assertions ran — `Test Files 34 passed` above `Tests 331 passed` is why a first-match regex
# would have reported 34. Its colour codes sit between the indent and the word `Tests`, so a run
# with colour on has to be stripped before the line can be read at all.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VITEST_TESTS = re.compile(r"^\s*Tests\s+(?:(\d+) failed \| )?(\d+) passed", re.MULTILINE)


def _vitest_tests(output: str) -> int:
    found = _VITEST_TESTS.search(_ANSI.sub("", output))
    if found is None:
        return 0
    failed = int(found.group(1)) if found.group(1) else 0
    return failed + int(found.group(2))


def fields_for(
    run: Run, code: int, output: str, seconds: float, change: ChangeSet
) -> dict[str, str]:
    if run.label == "pyright":
        return _pyright_fields(output, seconds, change)
    if run.label == "meizel":
        return {"meizel": _meizel_field(code, output, seconds)}
    if run.label == "vitest-app":
        ran = _vitest_tests(output)
        verdict = "ok" if code == 0 else f"failed({code})"
        return {"vitest-app": f"{verdict}({ran}tests)({spell_seconds(seconds)})"}
    if run.label == "debt":
        return {"debt": _debt_field(code, output, seconds)}
    if run.label == "docs-refs":
        return {"docs-refs": _docs_refs_field(code, output, seconds)}
    if run.label == "docs-okf":
        return {"docs-okf": _docs_okf_field(code, output, seconds)}
    if run.label == "refusal-loss":
        return {"refusal-loss": _refusal_field(output)}
    verdict = "ok" if code == 0 else f"failed({code})"
    return {run.label: f"{verdict}({spell_seconds(seconds)})"}


# meiva:note
# Why every layer no trigger ran is absent, said in this change's own terms: "no python touched"
# is a fact about the change, not an excuse, and the reader can check it against `scope=`.
def not_run_fields(change: ChangeSet, *, pyright_ran: bool) -> dict[str, str]:
    unread = {
        "pyright": "n/a(no-python-touched)",
        "err_in_this_diff": "n/a(no-python-touched)",
        "closure": "n/a(no-python-touched)",
        "ruff": "n/a(no-python-touched)",
        "vitest-app": "n/a(no-web-tree-file)",
        "next-build": "n/a(not-the-app-router)",
        "debt": "n/a(no-manifest-changed)",
        "refusal-loss": "n/a(no-net-debt-reduction)",
        "docs-refs": "n/a(no-doc-touched)",
        "docs-okf": "n/a(no-doc-touched)",
    }
    if pyright_ran:
        unread["closure"] = "n/a(the-tree-was-green)"
    elif "python" in kinds(change):
        unread["pyright"] = "0err(0s)"
        unread["err_in_this_diff"] = "0"
        unread["closure"] = "n/a(the-tree-was-green)"
        unread["ruff"] = "ok(0s)"
    return unread


def build_receipt(values: Mapping[str, str], sha: str, base: str) -> str:
    fields = [f"{label}={values.get(label, 'n/a(unfilled)')}" for label in FIELD_ORDER]
    return " ".join(
        (
            f"{RECEIPT_VERSION} @{sha}",
            f"base={base}",
            *fields,
            "generated=scripts/hooks/diff_checks.py",
        )
    )
