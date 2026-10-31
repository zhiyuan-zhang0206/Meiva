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
# meiva:note
# The commit that made the receipt a pre-merge gate (`#7990`, 21:22 on 2026-10-31). Everything at or
# before it predates the rule, so an audit that reaches further back reports the history of a gate
# that did not exist yet -- the first `--last 40` run produced 32 such "violations" and would have
# made the auditor's own first output noise.
GATE_EPOCH = "23bdc7ece90bcc13046cb4d301212718709e9a4b"
CHECKER_PATH = "scripts/hooks/diff_checks_receipt.py"
_PULL_REQUEST = re.compile(r"\(#(\d+)\)\s*$")
_TRAILER = re.compile(r"Meiva #\d+")
# meiva:note
# Who pushed the commit decides whether the receipt gate applies to it. The gate guards what the
# cluster lands; the user's own commits (#7996/#7997/#7998) carry no trailer and no receipt, and
# reporting them as violations would make the auditor's first output noise -- which is how a
# protection stops being read.
_CLUSTER_AUTHORS = frozenset({"zhiyuan-zhang0206"})
_USER_AUTHORS = frozenset({"wuji.zhangagain"})
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# meiva:note
# What each verdict means for the caller's exit code. A commit the tool could not judge is not a
# pass and not a violation: it is a hole in the audit, reported as its own class.
_VIOLATIONS = frozenset({"no-receipt", "rejected"})
# meiva:note
# A commit the audit could not judge -- `gh` unreadable, no checker in its tree, an author it cannot
# classify -- is a hole, and a hole is neither a pass nor a violation.
_UNJUDGED = frozenset({"unverified", "unknown"})


@dataclass(frozen=True)
class Landed:
    commit: str
    subject: str
    author: str = ""
    message: str = ""

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
# The commits to judge, oldest first, from a range or a count. Merge commits are skipped: this
# repository squashes every pull request into one commit, so a merge commit carries no subject of
# its own to read a pull request number from. The body comes along because the trailer that marks
# a cluster push lives there, and `\x01` ends each record so a multi-line body cannot run into the
# next one.
def _landed(root: Path, *, start: str | None, end: str, count: int | None) -> list[Landed]:
    arguments = ["log", "--no-merges", "--format=%H%x00%s%x00%ae%x00%b%x01"]
    arguments.append(f"{start or GATE_EPOCH}..{end}")
    if count is not None:
        arguments.append(f"--max-count={count}")
    output = subprocess.run(  # noqa: S603
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    landed: list[Landed] = []
    for entry in output.split("\x01"):
        fields = entry.split("\0", 3)
        if len(fields) < 4:
            continue
        landed.append(Landed(fields[0].strip(), fields[1], author=fields[2], message=fields[3]))
    return landed


# meiva:note
# Whether the receipt gate applies to a commit at all. The gate guards what the cluster lands: the
# user's own commits carry no trailer and no receipt, and reporting them as violations would make
# the auditor's first output noise -- which is how a protection stops being read.
# meiva:note
# The account a commit's author address names, without GitHub's numeric prefix: commits pushed
# through the web or a fork arrive as `87293881+login@users.noreply.github.com`.
def account(author: str) -> str:
    local = author.partition("@")[0]
    return local.split("+", 1)[-1]


def gate(landed: Landed, body: str) -> str:
    if _TRAILER.search(landed.message) or _TRAILER.search(body):
        return "cluster-agent"
    who = account(landed.author)
    if who in _CLUSTER_AUTHORS:
        return "cluster-agent"
    if who in _USER_AUTHORS:
        return "user-authored"
    return f"unknown({landed.author or 'no-author'})"


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
# because it resolves the repository root from its own path; `--repo-root`, `--head` and the body
# name the facts it cannot infer here. The body travels because a checker may ask the author to
# declare something in it (`baseline-red:`, the refusal-loss review line): a gate that read the
# line without the body would refuse every receipt whose honesty lives in the prose beside it.
def evaluate(checker: str, line: str, head: str, root: Path, body: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as workspace:
        nested = Path(workspace) / "a" / "b" / "c"
        nested.mkdir(parents=True)
        script = nested / "receipt.py"
        script.write_text(checker)
        receipt = Path(workspace) / "receipt.txt"
        receipt.write_text(line + "\n")
        body_file = Path(workspace) / "body.md"
        body_file.write_text(body)
        # meiva:note
        # The body is offered, not imposed: a checker older than `--body-file` would exit on an
        # unrecognised argument, and the audit judges every landed commit with the checker its own
        # tree carried — so the flag is passed only to the checkers that name it.
        body_arguments = ["--body-file", str(body_file)] if "--body-file" in checker else []
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(script),
                "--receipt-file",
                str(receipt),
                *body_arguments,
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
    klass = gate(landed, body)
    if klass.startswith("unknown("):
        return Judgement(landed, "unknown", klass)
    if klass == "user-authored":
        return Judgement(landed, "not-gated", "user-authored")
    lines = receipt_lines(body)
    if not lines:
        return Judgement(landed, "no-receipt", "the body carries no `diff-checks/v1 ` line")
    checker = _checker_source(root, landed.commit)
    if checker is None:
        return Judgement(landed, "unverified", f"the commit's tree carries no {CHECKER_PATH}")
    detail = "no receipt line passed"
    for line in lines:
        passed, detail = evaluate(checker, line, head, root, body)
        if passed:
            return Judgement(landed, "passed", "")
    return Judgement(landed, "rejected", detail)


def exit_code(judgements: Sequence[Judgement]) -> int:
    if any(judgement.verdict in _VIOLATIONS for judgement in judgements):
        return 1
    if any(judgement.verdict in _UNJUDGED for judgement in judgements):
        return 2
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from",
        dest="start",
        default=GATE_EPOCH,
        help=f"the commit to audit after (exclusive); default the gate itself, {GATE_EPOCH[:12]}",
    )
    parser.add_argument("--to", dest="end", default="origin/main", help="the last commit to audit")
    parser.add_argument("--last", type=int, help="audit this many commits ending at --to")
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.repo_root.resolve()
    landed = _landed(root, start=arguments.start, end=arguments.end, count=arguments.last)
    judgements = [judge_commit(root, item) for item in landed]
    for judgement in judgements:
        print(judgement.field())
    violations = [item for item in judgements if item.verdict in _VIOLATIONS]
    holes = [item for item in judgements if item.verdict in _UNJUDGED]
    outside = [item for item in judgements if item.verdict == "not-gated"]
    print(
        f"audit_merge_receipts: {len(judgements)} commits, {len(violations)} violation(s), "
        f"{len(holes)} unjudged, {len(outside)} outside the gate, "
        f"{len(judgements) - len(violations) - len(holes) - len(outside)} passed"
    )
    return exit_code(judgements)


if __name__ == "__main__":
    raise SystemExit(main())
