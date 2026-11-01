# meiva:note
# Attach the receipt a pull request did not carry when it merged -- or the one it must carry before
# it merges -- judged by the checker that pull request's own head carries, so what this writes is
# what `audit_merge_receipts.py` will later read. The block it owns is replaced, never stacked: two
# copies of one fact is how a re-sending watcher delivered one event 70 times (2026-11-01).
#
# A receipt is evidence about one tree, so a caller either names one it already has or lets this
# produce one from the tree pinned at the head. Nothing here invents a result, and a receipt the
# checker refuses leaves the body untouched -- a red written as green is worse than no line at all.
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.hooks.audit_merge_receipts import CHECKER_PATH, checker_at, evaluate, full_sha

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_PREFIX = "diff-checks/v1 "
MARK_BEGIN = "<!-- receipt-attach:begin -->"
MARK_END = "<!-- receipt-attach:end -->"
_OWNED = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", re.DOTALL)


@dataclass(frozen=True)
class Pull:
    number: int
    head: str
    body: str
    merged_at: str | None


# meiva:note
# One pull request as `gh` reports it. None means the tool could not read it, which is a hole in the
# record -- never a pass, and never a reason to write something instead.
def read_pull(root: Path, number: int) -> Pull | None:
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(number), "--json", "headRefOid,body,mergedAt"],
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
    if not head or body is None:
        return None
    merged = payload.get("mergedAt")
    return Pull(number, str(head), str(body), str(merged) if merged else None)


# meiva:note
# The block this tool owns, marked at both ends so a re-run can find and replace exactly its own
# addition. The note says which fact the line is: a receipt attached after the merge is a record of
# what was true, not a claim that the gate ran before the merge.
def block(receipt: str, note: str) -> str:
    return f"{MARK_BEGIN}\n{receipt}\n{note}\n{MARK_END}"


def attached(body: str, receipt: str, note: str) -> str:
    kept = _OWNED.sub("", body).rstrip()
    return (kept + "\n\n" if kept else "") + block(receipt, note) + "\n"


# meiva:note
# The receipt lines a body carries outside this tool's block: a hand-written receipt is the author's
# own evidence and is left where it is.
def outside_receipts(body: str) -> list[str]:
    return [
        line.strip()
        for line in _OWNED.sub("", body).splitlines()
        if line.strip().startswith(RECEIPT_PREFIX)
    ]


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()


# meiva:note
# What the note under the line says: before the merge the receipt is the gate's own output; after
# it, the same line is posted so the record can be checked, and says so in the same breath.
def note_for(pull: Pull, *, now: datetime) -> str:
    posted = now.astimezone().isoformat(timespec="seconds")
    if pull.merged_at is None:
        return f"(pre-merge receipt, posted {posted})"
    merged = _moment(pull.merged_at).strftime("%Y-%m-%d %H:%M:%S %z")
    return f"(after-the-fact: merged {merged}, receipt posted {posted})"


# meiva:note
# The candidate body, or None when the pinned checker refuses the line: the caller writes only what
# the checker would accept, so a red can never be posted as a green.
def attach_if_valid(
    checker: str, line: str, head: str, root: Path, body: str, note: str
) -> tuple[str | None, str]:
    candidate = attached(body, line, note)
    passed, detail = evaluate(checker, line, head, root, candidate)
    return (candidate if passed else None), detail


# meiva:note
# Merging is a separate decision from writing: it needs a body that verified and a pull request that
# is still open. Anything else stops here rather than pressing the button on unproven evidence.
def may_merge(pull: Pull, *, verified: bool) -> bool:
    return verified and pull.merged_at is None


# meiva:note
# The worktree checked out at this commit, or None: the receipt describes that tree, so it has to be
# the tree this tool runs the checks in rather than whichever checkout happened to be current.
def worktree_at(root: Path, head: str) -> Path | None:
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    current: Path | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree ") :])
        elif line.startswith("HEAD ") and line[len("HEAD ") :] == head:
            return current
    return None


def _python_for(tree: Path) -> str:
    candidate = tree / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


# meiva:note
# Produce a receipt from the tree at the head. A shell session's log belongs to whoever ran it, so
# the default source is the run itself: same entry point, same tree, same pin as the audit.
def produce_receipt(root: Path, head: str) -> str | None:
    tree = worktree_at(root, head)
    if tree is None:
        print(
            f"receipt_attach: no worktree is checked out at {head}; check it out, "
            "or pass --receipt-line/--receipt-file",
            file=sys.stderr,
        )
        return None
    result = subprocess.run(  # noqa: S603
        [_python_for(tree), str(tree / "scripts" / "hooks" / "diff_checks.py")],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(RECEIPT_PREFIX)
    ]
    if not lines:
        print(result.stderr[-2000:], file=sys.stderr)
        return None
    return lines[-1]


# meiva:note
# The body is written to a file under the repository's cache directory and handed to `gh` by path:
# a multi-kilobyte body on argv is a shell-quoting hazard and a visible secret surface, and the file
# also leaves behind what was posted if the edit itself fails.
def _write_body(root: Path, number: int, body: str) -> bool:
    written = root / ".cache" / "receipt-attach" / f"pr-{number}.md"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(body)
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "edit", str(number), "--body-file", str(written)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return False
    return True


# meiva:note
# Merge only after the body's own evidence verifies once more: the body read back from the server,
# not the string this process was holding. `--subject` is never passed -- it replaces the title and
# GitHub then appends no `(#n)`, which is how a landed commit stops naming its pull request.
def merge(root: Path, pull: Pull, checker: str, line: str, body: str) -> int:
    passed, detail = evaluate(checker, line, pull.head, root, body)
    if not may_merge(pull, verified=passed):
        print(f"receipt_attach: not merging -- {detail or 'already merged'}", file=sys.stderr)
        return 3
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "merge", str(pull.number), "--squash"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 3
    print(f"receipt_attach: merged #{pull.number}")
    return 0


def _receipt_line(arguments: argparse.Namespace, root: Path, head: str) -> str | None:
    if arguments.receipt_line is not None:
        return arguments.receipt_line.strip()
    if arguments.receipt_file is not None:
        return arguments.receipt_file.read_text().strip()
    return produce_receipt(root, head)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Attach a diff-checks receipt to a pull request.")
    parser.add_argument("--pr", type=int, required=True, help="the pull request number")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--receipt-file", type=Path, help="a file holding the receipt line")
    source.add_argument("--receipt-line", help="the receipt line itself")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="merge the pull request after the posted body verifies (default: only attach)",
    )
    parser.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = arguments.repo_root.resolve()
    pull = read_pull(root, arguments.pr)
    if pull is None:
        print(f"receipt_attach: gh could not read #{arguments.pr}", file=sys.stderr)
        return 2
    if not full_sha(pull.head):
        print(f"receipt_attach: the head {pull.head!r} is not a full sha", file=sys.stderr)
        return 2
    checker = checker_at(root, pull.head)
    if checker is None:
        print(f"receipt_attach: {pull.head} carries no {CHECKER_PATH}", file=sys.stderr)
        return 2
    for carried in outside_receipts(pull.body):
        passed, _ = evaluate(checker, carried, pull.head, root, pull.body)
        if passed:
            print("receipt_attach: the body already carries a receipt that passes")
            return merge(root, pull, checker, carried, pull.body) if arguments.merge else 0
    line = _receipt_line(arguments, root, pull.head)
    if not line:
        return 1
    note = note_for(pull, now=datetime.now().astimezone())
    candidate, detail = attach_if_valid(checker, line, pull.head, root, pull.body, note)
    if candidate is None:
        print(f"receipt_attach: the receipt was not attached -- {detail}", file=sys.stderr)
        return 1
    if not _write_body(root, pull.number, candidate):
        return 2
    posted = read_pull(root, pull.number)
    if posted is None:
        print("receipt_attach: gh could not read the edited body back", file=sys.stderr)
        return 2
    passed, detail = evaluate(checker, line, pull.head, root, posted.body)
    if not passed:
        print(f"receipt_attach: the posted body does not verify -- {detail}", file=sys.stderr)
        return 1
    print(f"receipt_attach: posted the receipt to #{pull.number}")
    return merge(root, posted, checker, line, posted.body) if arguments.merge else 0


if __name__ == "__main__":
    raise SystemExit(main())
