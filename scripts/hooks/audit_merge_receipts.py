# meiva:note
# Audit the receipts of commits that already landed. `docs/contributing.md` says a pull request
# carries one line produced by the checks its change needs, and until this script the only
# enforcement was the reviewer's memory: #7994, which edited the receipt checker itself, landed
# without one 42 minutes after the gate was written. A process rule whose only mechanism is
# remembering is not a gate.
#
# Judged by **the checker that commit carries**, never by today's: the vocabulary grew (`scope`,
# `docs-refs`, `docs-okf`), so an audit pinned to the current file calls honest history a violation
# -- #7990's receipt passes its own tree's checker and fails today's.

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_PREFIX = "diff-checks/v1 "
CHECKER_PATH = "scripts/hooks/diff_checks_receipt.py"
_PULL_REQUEST = re.compile(r"\(#(\d+)\)\s*$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# meiva:note
# What each verdict means for the caller's exit code. A commit the tool could not judge is not a
# pass and not a violation: it is a hole in the audit, reported as its own class.
_VIOLATIONS = frozenset({"no-receipt", "rejected"})


@dataclass(frozen=True)
class Landed:
    commit: str
    subject: str

    @property
    def pull_request(self) -> int | None:
        found = _PULL_REQUEST.search(self.subject)
        return int(found.group(1)) if found else None


@dataclass(frozen=True)
class Judgement:
    landed: Landed
    verdict: str
    detail: str

    def field(self) -> str:
        number = f"#{self.landed.pull_request}" if self.landed.pull_request else "-"
        return f"{self.landed.commit} {number:>7} {self.verdict:<15} {self.detail}".rstrip()


# meiva:note
# The commits to judge, newest last, from a range or a count. Merge commits are skipped: this
# repository squashes every pull request into one commit, so a merge commit carries no subject of
# its own to read a pull request number from.
def _landed(root: Path, *, start: str | None, end: str, count: int | None) -> list[Landed]:
    arguments = ["log", "--no-merges", "--format=%H%x00%s"]
    arguments += [f"--max-count={count}", end] if count is not None else [f"{start}..{end}"]
    lines = subprocess.run(  # noqa: S603
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [Landed(*line.split("\0", 1)) for line in lines if line]


# meiva:note
# The head commit and body of one pull request, or None when `gh` cannot read it.
def _pull_request(root: Path, number: int) -> tuple[str, str] | None:
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(number), "--json", "headRefOid,body"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    head, body = payload.get("headRefOid"), payload.get("body")
    return (str(head), str(body)) if head and body is not None else None


# meiva:note
# The checker compares the receipt's head against the string it is given, so a truncated sha reads
# as "this receipt belongs to another commit" -- a false verdict that looks like the author's
# mistake. `gh pr view` already reports the full object id; this refuses anything shorter instead
# of passing it on.
def full_sha(value: str) -> bool:
    return bool(_FULL_SHA.match(value))


# meiva:note
# The receipt lines a pull request body carries, in the order they appear.
def receipt_lines(body: str) -> list[str]:
    return [line.strip() for line in body.splitlines() if line.strip().startswith(RECEIPT_PREFIX)]


# meiva:note
# The receipt checker as it stood in that commit, or None when it carried none.
def _checker_source(root: Path, commit: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{commit}:{CHECKER_PATH}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


# meiva:note
# Run one receipt line through one checker. The checker is written into a deep temporary directory
# because it resolves the repository root from its own path; `--repo-root` and `--head` name the
# two facts it cannot infer here.
def evaluate(checker: str, line: str, head: str, root: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as workspace:
        nested = Path(workspace) / "a" / "b" / "c"
        nested.mkdir(parents=True)
        script = nested / "receipt.py"
        script.write_text(checker)
        receipt = Path(workspace) / "receipt.txt"
        receipt.write_text(line + "\n")
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(script),
                "--receipt-file",
                str(receipt),
                "--head",
                head,
                "--repo-root",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    detail = " ".join(result.stderr.split()) or f"exit={result.returncode}"
    return result.returncode == 0, detail


# meiva:note
# One commit's verdict: passed, no-receipt, rejected, no-pull-request, or unverified.
def judge_commit(root: Path, landed: Landed) -> Judgement:
    number = landed.pull_request
    if number is None:
        return Judgement(landed, "no-pull-request", "the subject names no pull request")
    found = _pull_request(root, number)
    if found is None:
        return Judgement(landed, "unverified", "gh could not read the pull request")
    head, body = found
    if not full_sha(head):
        return Judgement(landed, "unverified", f"the pull request head {head!r} is not a full sha")
    lines = receipt_lines(body)
    if not lines:
        return Judgement(landed, "no-receipt", "the body carries no `diff-checks/v1 ` line")
    checker = _checker_source(root, landed.commit)
    if checker is None:
        return Judgement(landed, "unverified", f"the commit's tree carries no {CHECKER_PATH}")
    detail = "no receipt line passed"
    for line in lines:
        passed, detail = evaluate(checker, line, head, root)
        if passed:
            return Judgement(landed, "passed", "")
    return Judgement(landed, "rejected", detail)


def exit_code(judgements: Sequence[Judgement]) -> int:
    if any(judgement.verdict in _VIOLATIONS for judgement in judgements):
        return 1
    if any(judgement.verdict == "unverified" for judgement in judgements):
        return 2
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", help="the commit to audit after (exclusive)")
    parser.add_argument("--to", dest="end", default="origin/main", help="the last commit to audit")
    parser.add_argument("--last", type=int, help="audit this many commits ending at --to")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.repo_root.resolve()
    if arguments.last is None and not arguments.start:
        print("audit_merge_receipts: --from or --last is required", file=sys.stderr)
        return 2
    landed = _landed(root, start=arguments.start, end=arguments.end, count=arguments.last)
    judgements = [judge_commit(root, item) for item in landed]
    for judgement in judgements:
        print(judgement.field())
    violations = [item for item in judgements if item.verdict in _VIOLATIONS]
    unverified = [item for item in judgements if item.verdict == "unverified"]
    print(
        f"audit_merge_receipts: {len(judgements)} commits, {len(violations)} violation(s), "
        f"{len(unverified)} unverified, {len(judgements) - len(violations) - len(unverified)} passed"
    )
    return exit_code(judgements)


if __name__ == "__main__":
    raise SystemExit(main())
