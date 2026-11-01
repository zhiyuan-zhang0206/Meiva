# meiva:note
# What the test layer reads: the report pytest wrote, the counts it holds, and the verdict a
# truncated run earns. It lives apart from the scheduling in `diff_checks.py` because reading a run
# and deciding which runs to make are different jobs, and the file that does both reached the
# 800-line ceiling the moment this reader gained its third state.

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


def spell_seconds(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}m{whole % 60:02d}s" if whole >= 60 else f"{whole}s"


# meiva:note
# What a pytest run reported, read from the report pytest wrote rather than from its summary line.
#
# A summary is the runner's claim about a run; the report is the run. When a worker dies mid-run
# pytest still prints a summary of what happened to finish, so a receipt that parses it reports
# `failed=18tests` for a selection of 1990 files in which part of the run never happened -- a
# number no reader can tell from eighteen real failures (#8041, 2026-11-01).
# meiva:note
# Three states, not two: a run is counted, or it is broken for a named reason. The dataclass keeps
# the four receipt fields together so no caller can publish one without the others.
@dataclass(frozen=True)
class PytestCounts:
    values: dict[str, str]
    broken: bool


# meiva:note
# The reason a run is broken, kept in the `tests` field so the reader never has to infer it from a
# suspiciously small number:
#
# - `broken(no-report)`: the run left no report at all -- it died before pytest could write one.
# - `broken(short-run)`: the report holds fewer results than files were selected, so part of the
#   selection never executed.
#
# Both keep the receipt non-zero (`check()` folds `broken` into the failure the caller returns):
# a truncated run is not evidence of anything, least of all of a green tree. Only a whole report
# is counted, and only then does a failure count mean "these tests failed".
#
# The denominator is the number of FILES the selector handed over, and the floor sits at one result
# per file -- far below the ~18 results a backend test file yields. A module of fixtures only, a
# `collect_ignore`, or a file-level skip contributes no result and must not read as a truncated
# run: at that floor one silent file cannot move the verdict, which is why the comparison is
# against the file count and not against a per-file average. A run that loses most of its results
# (29 out of 1990 files, 2026-11-01) is what this catches.
def pytest_counts(report: Path, *, selected_files: int, seconds: float) -> PytestCounts:
    def broken(reason: str) -> PytestCounts:
        return PytestCounts(
            {
                "tests": f"broken({reason})",
                "passed": "n/a(broken)",
                "failed": "n/a(broken)",
                "errors": "n/a(broken)",
            },
            broken=True,
        )

    try:
        # meiva:note
        # Our own file, written by the pytest process this runner started in a directory it made:
        # nothing here parses a document from outside the run.
        root = ElementTree.parse(report).getroot()  # noqa: S314
    except (OSError, ElementTree.ParseError):
        return broken("no-report")
    cases = list(root.iter("testcase"))
    failures = sum(1 for case in cases if case.find("failure") is not None)
    errors = sum(1 for case in cases if case.find("error") is not None)
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    if len(cases) < selected_files:
        return broken("short-run")
    return PytestCounts(
        {
            "tests": f"RAN({selected_files}files)",
            "passed": f"{len(cases) - failures - errors - skipped}tests({spell_seconds(seconds)})",
            "failed": f"{failures}tests",
            "errors": f"{errors}tests",
        },
        broken=False,
    )


# meiva:note
# The command line the reader above expects: pytest's own report file, named so this run's reader
# cannot pick up another run's.
def pytest_argv(report: Path, tests: tuple[str, ...]) -> tuple[str, ...]:
    return (sys.executable, "-m", "pytest", "-q", f"--junitxml={report}", *tests)
