# meiva:note
# Check the one line a pull request body carries: that it was produced for this head, that every
# field a reader relies on is present, that every field states a result or says why it has none,
# that a layer whose output could not be read is not passed off as green, that a blind selection
# names its fallback, and that a refusal-loss count found unreplaced refusals carries the
# reviewer's line. It judges presence and shape, never whether a reason is a good one — grading
# reasons teaches authors to write reasons that pass.
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RECEIPT_VERSION = "diff-checks/v1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# meiva:note
# The fields a reader trusts, all of them present in every receipt: a check this change cannot
# need says `n/a(<why>)` there rather than disappearing, so "not applicable" is a value a reader
# can see instead of an absence they have to interpret.
REQUIRED_FIELDS = (
    "base",
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
    "generated",
)

_REFUSAL_LINE = re.compile(r"refusal-loss:.*reviewed:")
_UNREPLACED = re.compile(r"unreplaced=(\d+)")
_PASSED = re.compile(r"\d+tests\(")
_FAILED = re.compile(r"\d+tests$")
_COUNTED = re.compile(r"(\d+)tests$")
# meiva:note
# A red receipt states its reds, and the body says which of them were already there: counts alone
# cannot separate "this change broke four tests" from "four tests were red before it started".
# One line, the same shape the refusal-loss and policy-removal declarations already use.
_BASELINE_RED = re.compile(r"(?m)^baseline-red:")


# meiva:note
# What a count field says, and 0 for a field that reports no count at all: `n/a(no-test-ran)` and
# `unreadable(...)` are answered by their own rules, not by this one.
def _counted(value: str) -> int:
    found = _COUNTED.search(value)
    return int(found.group(1)) if found else 0


@dataclass(frozen=True)
class Receipt:
    head: str
    fields: dict[str, str]

    def get(self, name: str) -> str:
        return self.fields.get(name, "")


def parse(line: str) -> Receipt:
    parts = line.split()
    if not parts or parts[0] != RECEIPT_VERSION:
        raise ValueError(f"not a {RECEIPT_VERSION} receipt")
    if len(parts) < 2 or not parts[1].startswith("@"):
        raise ValueError("the receipt names no head commit")
    fields: dict[str, str] = {}
    for part in parts[2:]:
        name, _, value = part.partition("=")
        fields[name] = value
    return Receipt(head=parts[1][1:], fields=fields)


# meiva:note
# An absence states its reason in parentheses -- `n/a(no python touched)`, `skipped(--skip-tests)`
# -- because the bare word `none` cannot be told from a layer that ran and found nothing.
def absence_names_reason(value: str) -> bool:
    return not value.startswith(("n/a(", "skipped(")) or value.endswith(")")


# meiva:note
# What a reader would otherwise have to reconstruct by hand: a field with no result and no reason,
# a layer whose output the runner could not read, a passed count beside a skipped run, a run that
# does not say how many tests passed and failed, a blind selection without its fallback, and a
# refusal-loss line the author did not answer.
# meiva:note
# The fields themselves: present, stating something, and stating it readably. Each finding names
# the fields it is about, so an author can act without re-reading the whole line.
def _presence(receipt: Receipt) -> list[str]:
    problems: list[str] = []
    missing = [name for name in REQUIRED_FIELDS if name not in receipt.fields]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    silent = sorted(name for name, value in receipt.fields.items() if value in ("", "none"))
    if silent:
        problems.append(
            f"fields with neither a result nor a reason: {', '.join(silent)} -- "
            f"write n/a(<why>) or skipped(<why>)"
        )
    # meiva:note
    # A read failure is a verdict about the field, not a word in it: `unreadable(...)` is refused,
    # while a reason that happens to mention the word -- `n/a(manifest-unreadable-on-this-ref)` --
    # is the author telling the truth and stays theirs to word.
    unread = sorted(
        name for name, value in receipt.fields.items() if value.startswith("unreadable")
    )
    if unread:
        problems.append(f"the runner could not read: {', '.join(unread)}")
    reasons = sorted(
        name for name, value in receipt.fields.items() if not absence_names_reason(value)
    )
    if reasons:
        problems.append(f"an absence must name its reason in parentheses: {', '.join(reasons)}")
    return problems


# meiva:note
# Each claim here is checkable against another field: a run that skipped cannot report a count, a
# run that executed tests must report both, and a blind selection must name the fallback it ran.
def _run_claims(receipt: Receipt) -> list[str]:
    problems: list[str] = []
    tests = receipt.get("tests")
    # meiva:note
    # A broken run is not a red one and not a green one: it is the absence of evidence, so the gate
    # refuses it by name. Without this the receipt could say `broken(short-run)` and still be
    # accepted, because none of the other rules look at `tests` unless it starts with RAN.
    if tests.startswith("broken"):
        problems.append(
            f"the run is {tests}: it did not execute the selection it was asked for, so it is "
            f"neither red nor green -- rerun it (or run a smaller selection) before merging"
        )
    if tests.startswith("skipped") and not receipt.get("passed").startswith("n/a("):
        problems.append("a skipped run must not also report a passed count")
    if tests.startswith("RAN") and not (
        _PASSED.search(receipt.get("passed"))
        and _FAILED.search(receipt.get("failed"))
        and _FAILED.search(receipt.get("errors"))
    ):
        problems.append(
            "a run that executed tests must report `passed=<n>tests(<time>)`, `failed=<n>tests` "
            "and `errors=<n>tests`"
        )
    blind = receipt.get("blind")
    if not blind.startswith(("n/a(", "BLIND(")):
        problems.append("`blind=` is either n/a(<why>) or BLIND(<the files it could not map>)")
    if blind.startswith("BLIND(") and not receipt.get("fallback").startswith("RAN("):
        problems.append("a blind selection needs the fallback run it named")
    return problems


def findings(receipt: Receipt, *, head: str, body: str) -> list[str]:
    problems: list[str] = []
    if head and receipt.head != head:
        problems.append(f"the receipt was produced for {receipt.head}, not for {head}")
    problems.extend(_presence(receipt))
    problems.extend(_run_claims(receipt))
    red = sum(_counted(receipt.get(name)) for name in ("failed", "errors"))
    if red and not _BASELINE_RED.search(body):
        problems.append(
            f"the run reported {red} failing or errored test(s); the body needs a "
            "`baseline-red: <the node ids, and why they are not this change>` line"
        )
    unreplaced = _UNREPLACED.search(receipt.get("refusal-loss"))
    if unreplaced is not None and int(unreplaced.group(1)) > 0 and not _REFUSAL_LINE.search(body):
        problems.append(
            "the receipt found unreplaced refusals; the body needs a "
            "`refusal-loss: ... reviewed:` line"
        )
    return problems


def head_of(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-file", type=Path, help="the line to check; default stdin")
    parser.add_argument("--body-file", type=Path, help="the pull request body to check it against")
    parser.add_argument("--head", default="", help="the head commit to expect; default this tree")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    line = (
        arguments.receipt_file.read_text()
        if arguments.receipt_file is not None
        else sys.stdin.read()
    )
    body = arguments.body_file.read_text() if arguments.body_file is not None else ""
    head = arguments.head or head_of(arguments.repo_root.resolve())
    try:
        receipt = parse(line.strip())
    except ValueError as error:
        print(f"diff-checks receipt: {error}", file=sys.stderr)
        return 1
    problems = findings(receipt, head=head, body=body)
    for problem in problems:
        print(f"diff-checks receipt: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
