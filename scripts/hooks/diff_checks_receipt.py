# meiva:note
# Check the one line a pull request body carries: that it was produced for this head, that every
# field a reader relies on is present, that a layer it did not run says why, that a blind
# selection named its fallback, and that a refusal-loss count found unreplaced refusals carries
# the reviewer's line. It judges presence and shape, never whether a reason is a good one —
# grading reasons teaches authors to write reasons that pass.
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
# The fields a reader trusts. `passed` and `failed` are absent when the tests did not run, so
# they are checked against `tests=` rather than required outright.
REQUIRED_FIELDS = (
    "base",
    "pyright",
    "err_in_this_diff",
    "ruff",
    "selector",
    "tests",
    "fallback",
    "blind",
    "vitest",
    "meizel",
    "debt",
    "refusal-loss",
    "skipped",
    "generated",
)

_REFUSAL_LINE = re.compile(r"refusal-loss:.*reviewed:")
_UNREPLACED = re.compile(r"unreplaced=(\d+)")


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


def reasons_named(value: str) -> bool:
    if value in ("", "none"):
        return True
    return all(item.endswith(")") and "(" in item for item in value.split(","))


# meiva:note
# What a reader would otherwise have to reconstruct by hand: a blind selection without its
# fallback, a passed count beside a skipped run, and a refusal-loss line the author did not answer.
def findings(receipt: Receipt, *, head: str, body: str) -> list[str]:
    problems: list[str] = []
    if head and receipt.head != head:
        problems.append(f"the receipt was produced for {receipt.head}, not for {head}")
    missing = [name for name in REQUIRED_FIELDS if name not in receipt.fields]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    tests = receipt.get("tests")
    if tests.startswith("SKIPPED") and receipt.get("passed"):
        problems.append("a skipped run must not also report a passed count")
    if not reasons_named(receipt.get("skipped")):
        problems.append("a skipped layer must name its reason in parentheses")
    if receipt.get("blind") not in ("", "none") and not receipt.get("fallback").startswith("RAN("):
        problems.append("a blind selection needs the fallback run it named")
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
